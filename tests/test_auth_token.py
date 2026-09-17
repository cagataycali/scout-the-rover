"""issue_token(ttl=) mints long-lived service tokens verified by the same code path."""
import os, sys, time, tempfile, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
os.environ.setdefault("SCOUT_AUTH_STORE", os.path.join(tempfile.mkdtemp(), "auth.json"))
import auth  # noqa: E402


def test_default_ttl_matches_env():
    tok = auth.issue_token("u", "n")
    c = auth.verify_token(tok)
    assert c["sub"] == "u" and c["exp"] - c["iat"] == auth.TOKEN_TTL


def test_service_token_long_ttl_verifies():
    year = 365 * 86400
    tok = auth.issue_token("tiny-ios-body", "phone", ttl=year)
    c = auth.verify_token(tok)
    assert c["exp"] - c["iat"] == year
    assert c["exp"] > time.time() + 300 * 86400


def test_ttl_floor_is_one_second():
    tok = auth.issue_token("x", ttl=0)
    c = auth.verify_token(tok)
    assert c["exp"] - c["iat"] == 1
