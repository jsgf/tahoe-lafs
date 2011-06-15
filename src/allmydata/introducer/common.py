
import re, simplejson
from allmydata.util.ecdsa import VerifyingKey
from allmydata.util import base32, hashutil

def make_index(ann_d, key_s):
    """Return something that can be used as an index (e.g. a tuple of
    strings), such that two messages that refer to the same 'thing' will have
    the same index. This is a tuple of (service-name, signing-key) for signed
    announcements, or (service-name, tubid) for unsigned announcements."""

    service_name = str(ann_d["service-name"])
    if key_s:
        return (service_name, key_s)
    else:
        tubid = get_tubid_string_from_ann_d(ann_d)
        return (service_name, tubid)

def get_tubid_string_from_ann_d(ann_d):
    return get_tubid_string(str(ann_d["FURL"]))

def get_tubid_string(furl):
    m = re.match(r'pb://(\w+)@', furl)
    assert m
    return m.group(1).lower()

def convert_announcement_v1_to_v2(ann_t):
    (furl, service_name, ri_name, nickname, ver, oldest) = ann_t
    assert type(furl) is str
    assert type(service_name) is str
    assert type(ri_name) is str
    assert type(nickname) is str
    assert type(ver) is str
    assert type(oldest) is str
    ann_d = {"version": 0,
             "service-name": service_name,
             "FURL": furl,
             "remoteinterface-name": ri_name,

             "nickname": nickname.decode("utf-8"),
             "app-versions": {},
             "my-version": ver,
             "oldest-supported": oldest,
             }
    return simplejson.dumps( (simplejson.dumps(ann_d), None, None) )

def convert_announcement_v2_to_v1(ann_v2):
    (msg, sig, pubkey) = simplejson.loads(ann_v2)
    ann_d = simplejson.loads(msg)
    assert ann_d["version"] == 0
    ann_t = (str(ann_d["FURL"]), str(ann_d["service-name"]),
             str(ann_d["remoteinterface-name"]),
             ann_d["nickname"].encode("utf-8"),
             str(ann_d["my-version"]),
             str(ann_d["oldest-supported"]),
             )
    return ann_t


def sign(ann_d, sk):
    # returns (bytes, None, None) or (bytes, str, str)
    msg = simplejson.dumps(ann_d).encode("utf-8")
    if not sk:
        return (msg, None, None)
    vk = sk.get_verifying_key()
    sig = sk.sign(msg, hashfunc=hashutil.SHA256)
    return (msg, "v0-"+base32.b2a(sig), "v0-"+base32.b2a(vk.to_string()))

class UnknownKeyError(Exception):
    pass

def unsign(ann_t):
    (msg_s, sig_vs, key_vs) = ann_t
    key_s = None
    if sig_vs and key_vs:
        if not sig_vs.startswith("v0-"):
            raise UnknownKeyError("only v0- signatures recognized")
        if not key_vs.startswith("v0-"):
            raise UnknownKeyError("only v0- keys recognized")
        key_s = key_vs[3:]
        key = VerifyingKey.from_string(base32.a2b(key_s))
        key.verify(base32.a2b(sig_vs[3:]), msg_s, hashfunc=hashutil.SHA256)
    msg = simplejson.loads(msg_s.decode("utf-8"))
    return (msg, key_s)
