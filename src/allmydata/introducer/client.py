
import time, simplejson
from zope.interface import implements
from twisted.application import service
from foolscap.api import Referenceable, eventually, RemoteInterface, Violation
from allmydata.interfaces import InsufficientVersionError
from allmydata.introducer.interfaces import IIntroducerClient, \
     RIIntroducerSubscriberClient_v1, RIIntroducerSubscriberClient_v2
from allmydata.introducer.common import sign, unsign, make_index, \
     convert_announcement_v1_to_v2, convert_announcement_v2_to_v1
from allmydata.util import log, idlib
from allmydata.util.rrefutil import add_version_to_remote_reference
from allmydata.util.ecdsa import BadSignatureError

class ClientAdapter_v1(Referenceable): # for_v1
    """I wrap a v2 IntroducerClient to make it look like a v1 client, so it
    can be attached to an old server."""
    implements(RIIntroducerSubscriberClient_v1)

    def __init__(self, original):
        self.original = original

    def remote_announce(self, announcements):
        lp = self.original.log("received %d announcements (v1)" %
                               len(announcements))
        anns_v1 = set([convert_announcement_v1_to_v2(ann_v1)
                       for ann_v1 in announcements])
        return self.original.got_announcements(anns_v1, lp)

    def remote_set_encoding_parameters(self, parameters):
        self.original.remote_set_encoding_parameters(parameters)

class RIStubClient(RemoteInterface): # for_v1
    """Each client publishes a service announcement for a dummy object called
    the StubClient. This object doesn't actually offer any services, but the
    announcement helps the Introducer keep track of which clients are
    subscribed (so the grid admin can keep track of things like the size of
    the grid and the client versions in use. This is the (empty)
    RemoteInterface for the StubClient."""

class StubClient(Referenceable): # for_v1
    implements(RIStubClient)


class IntroducerClient(service.Service, Referenceable):
    implements(RIIntroducerSubscriberClient_v2, IIntroducerClient)

    def __init__(self, tub, introducer_furl,
                 nickname, my_version, oldest_supported,
                 app_versions):
        self._tub = tub
        self.introducer_furl = introducer_furl

        assert type(nickname) is unicode
        self._nickname = nickname
        self._my_version = my_version
        self._oldest_supported = oldest_supported
        self._app_versions = app_versions

        self._my_subscriber_info = { "version": 0,
                                     "nickname": self._nickname,
                                     "app-versions": self._app_versions,
                                     "my-version": self._my_version,
                                     "oldest-supported": self._oldest_supported,
                                     }
        self._stub_client = None # for_v1
        self._stub_client_furl = None

        self._published_announcements = {}
        self._canary = Referenceable()

        self._publisher = None

        self._local_subscribers = [] # (servicename,cb,args,kwargs) tuples
        self._subscribed_service_names = set()
        self._subscriptions = set() # requests we've actually sent

        # _current_announcements remembers one announcement per
        # (servicename,serverid) pair. Anything that arrives with the same
        # pair will displace the previous one. This stores tuples of
        # (unpacked announcement dictionary, verifyingkey, rxtime). The ann_d
        # dicts can be compared for equality to distinguish re-announcement
        # from updates. It also provides memory for clients who subscribe
        # after startup.
        self._current_announcements = {}

        self.encoding_parameters = None

        # hooks for unit tests
        self._debug_counts = {
            "inbound_message": 0,
            "inbound_announcement": 0,
            "wrong_service": 0,
            "duplicate_announcement": 0,
            "update": 0,
            "new_announcement": 0,
            "outbound_message": 0,
            }
        self._debug_outstanding = 0

    def _debug_retired(self, res):
        self._debug_outstanding -= 1
        return res

    def startService(self):
        service.Service.startService(self)
        self._introducer_error = None
        rc = self._tub.connectTo(self.introducer_furl, self._got_introducer)
        self._introducer_reconnector = rc
        def connect_failed(failure):
            self.log("Initial Introducer connection failed: perhaps it's down",
                     level=log.WEIRD, failure=failure, umid="c5MqUQ")
        d = self._tub.getReference(self.introducer_furl)
        d.addErrback(connect_failed)

    def _got_introducer(self, publisher):
        self.log("connected to introducer, getting versions")
        default = { "http://allmydata.org/tahoe/protocols/introducer/v1":
                    { },
                    "application-version": "unknown: no get_version()",
                    }
        d = add_version_to_remote_reference(publisher, default)
        d.addCallback(self._got_versioned_introducer)
        d.addErrback(self._got_error)

    def _got_error(self, f):
        # TODO: for the introducer, perhaps this should halt the application
        self._introducer_error = f # polled by tests

    def _got_versioned_introducer(self, publisher):
        self.log("got introducer version: %s" % (publisher.version,))
        # we require a V1 introducer
        needed = "http://allmydata.org/tahoe/protocols/introducer/v1"
        if needed not in publisher.version:
            raise InsufficientVersionError(needed, publisher.version)
        self._publisher = publisher
        publisher.notifyOnDisconnect(self._disconnected)
        self._maybe_publish()
        self._maybe_subscribe()

    def _disconnected(self):
        self.log("bummer, we've lost our connection to the introducer")
        self._publisher = None
        self._subscriptions.clear()

    def log(self, *args, **kwargs):
        if "facility" not in kwargs:
            kwargs["facility"] = "tahoe.introducer.client"
        return log.msg(*args, **kwargs)

    def subscribe_to(self, service_name, cb, *args, **kwargs):
        self._local_subscribers.append( (service_name,cb,args,kwargs) )
        self._subscribed_service_names.add(service_name)
        self._maybe_subscribe()
        for (servicename,nodeid),(ann_d,key,when) in self._current_announcements.items():
            if servicename == service_name:
                eventually(cb, nodeid, ann_d)

    def _maybe_subscribe(self):
        if not self._publisher:
            self.log("want to subscribe, but no introducer yet",
                     level=log.NOISY)
            return
        for service_name in self._subscribed_service_names:
            if service_name not in self._subscriptions:
                # there is a race here, but the subscription desk ignores
                # duplicate requests.
                self._subscriptions.add(service_name)
                self._debug_outstanding += 1
                d = self._publisher.callRemote("subscribe_v2",
                                               self, service_name,
                                               self._my_subscriber_info)
                d.addBoth(self._debug_retired)
                d.addErrback(self._subscribe_handle_v1, service_name) # for_v1
                d.addErrback(log.err, facility="tahoe.introducer.client",
                             level=log.WEIRD, umid="2uMScQ")

    def _subscribe_handle_v1(self, f, service_name): # for_v1
        f.trap(Violation, NameError)
        # they don't have a 'subscribe_v2' method: must be a v1 introducer.
        # Fall back to the v1 'subscribe' method, using a client adapter.
        ca = ClientAdapter_v1(self)
        self._debug_outstanding += 1
        d = self._publisher.callRemote("subscribe", ca, service_name)
        d.addBoth(self._debug_retired)
        # We must also publish an empty 'stub_client' object, so the
        # introducer can count how many clients are connected and see what
        # versions they're running.
        if not self._stub_client_furl:
            self._stub_client = sc = StubClient()
            self._stub_client_furl = self._tub.registerReference(sc)
        def _publish_stub_client(ignored):
            ri_name = RIStubClient.__remote_name__
            self.publish(self._stub_client_furl, "stub_client", ri_name)
        d.addCallback(_publish_stub_client)
        return d

    def create_announcement(self, furl, service_name, remoteinterface_name,
                            signing_key=None):
        ann_d = {"version": 0,
                 "service-name": service_name,
                 "FURL": furl,
                 "remoteinterface-name": remoteinterface_name,

                 "nickname": self._nickname,
                 "app-versions": self._app_versions,
                 "my-version": self._my_version,
                 "oldest-supported": self._oldest_supported,
                 }
        return simplejson.dumps(sign(ann_d, signing_key))


    def publish(self, furl, service_name, remoteinterface_name,
                signing_key=None):
        ann = self.create_announcement(furl, service_name, remoteinterface_name,
                                       signing_key)
        self._published_announcements[service_name] = ann
        self._maybe_publish()

    def _maybe_publish(self):
        if not self._publisher:
            self.log("want to publish, but no introducer yet", level=log.NOISY)
            return
        # this re-publishes everything. The Introducer ignores duplicates
        for ann in self._published_announcements.values():
            self._debug_counts["outbound_message"] += 1
            self._debug_outstanding += 1
            d = self._publisher.callRemote("publish_v2", ann, self._canary)
            d.addBoth(self._debug_retired)
            d.addErrback(self._handle_v1_publisher, ann) # for_v1
            d.addErrback(log.err, ann=ann, facility="tahoe.introducer.client",
                         level=log.WEIRD, umid="xs9pVQ")

    def _handle_v1_publisher(self, f, ann): # for_v1
        f.trap(Violation, NameError)
        # they don't have the 'publish_v2' method, so fall back to the old
        # 'publish' method (which takes an unsigned tuple of bytestrings)
        self.log("falling back to publish_v1",
                 level=log.UNUSUAL, umid="9RCT1A", failure=f)
        ann_v1 = convert_announcement_v2_to_v1(ann)
        self._debug_outstanding += 1
        d = self._publisher.callRemote("publish", ann_v1)
        d.addBoth(self._debug_retired)
        return d


    def remote_announce_v2(self, announcements):
        lp = self.log("received %d announcements (v2)" % len(announcements))
        return self.got_announcements(announcements, lp)

    def got_announcements(self, announcements, lp=None):
        # this is the common entry point for both v1 and v2 announcements
        self._debug_counts["inbound_message"] += 1
        for ann_s in announcements:
            try:
                ann_d, key = unsign(ann_s) # might raise bad-sig error
            except BadSignatureError:
                self.log("bad signature on inbound announcement: %s" % (ann_s,),
                         parent=lp, level=log.WEIRD, umid="ZAU15Q")
                # process other announcements that arrived with the bad one
                continue

            self._process_announcement(ann_d, key)

    def _process_announcement(self, ann_d, key):
        self._debug_counts["inbound_announcement"] += 1
        service_name = str(ann_d["service-name"])
        if service_name not in self._subscribed_service_names:
            self.log("announcement for a service we don't care about [%s]"
                     % (service_name,), level=log.UNUSUAL, umid="dIpGNA")
            self._debug_counts["wrong_service"] += 1
            return
        # for ASCII values, simplejson might give us unicode *or* bytes
        if "nickname" in ann_d and isinstance(ann_d["nickname"], str):
            ann_d["nickname"] = unicode(ann_d["nickname"])
        nick_s = ann_d.get("nickname",u"").encode("utf-8")
        lp2 = self.log(format="announcement for nickname '%(nick)s', service=%(svc)s: %(ann)s",
                       nick=nick_s, svc=service_name, ann=ann_d, umid="BoKEag")

        index = make_index(ann_d, key)
        nodeid = index[1]
        nodeid_s = idlib.nodeid_b2a(nodeid)

        # is this announcement a duplicate?
        if self._current_announcements.get(index, [None]*3)[0] == ann_d:
            self.log(format="reannouncement for [%(service)s]:%(nodeid)s, ignoring",
                     service=service_name, nodeid=nodeid_s,
                     parent=lp2, level=log.UNUSUAL, umid="B1MIdA")
            self._debug_counts["duplicate_announcement"] += 1
            return
        # does it update an existing one?
        if index in self._current_announcements:
            self._debug_counts["update"] += 1
            self.log("replacing old announcement: %s" % (ann_d,),
                     parent=lp2, level=log.NOISY, umid="wxwgIQ")
        else:
            self._debug_counts["new_announcement"] += 1
            self.log("new announcement[%s]" % service_name,
                     parent=lp2, level=log.NOISY)

        self._current_announcements[index] = (ann_d, key, time.time())
        # note: we never forget an index, but we might update its value

        for (service_name2,cb,args,kwargs) in self._local_subscribers:
            if service_name2 == service_name:
                eventually(cb, nodeid, ann_d, *args, **kwargs)

    def remote_set_encoding_parameters(self, parameters):
        self.encoding_parameters = parameters

    def connected_to_introducer(self):
        return bool(self._publisher)
