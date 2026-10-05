import pytest
from datetime import datetime, timezone
import json

def test_zero_length_window_is_closed():
    # Test that a zero-length window does not evaluate to "open"
    from multiagents.scheduler.windows import evaluate, prepare
    prepare("UTC")
    prepare("Europe/Paris")
    spec = {"days": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"], "ranges": ["10:00-10:00"]}
    encoded = json.dumps([spec])
    # 10:30 UTC
    instant = 4 * 86400 + 10 * 3600 + 1800
    res = evaluate(encoded, "UTC", instant)
    assert not res["open"], "Zero-length window evaluated as open"

def test_invalid_timezone_uses_default_or_fails():
    # If a timezone is invalid, it is cached as None.
    # evaluate() will then pass zone=None to contains() and boundaries().
    # datetime.fromtimestamp(instant, None) returns a naive datetime in local system time.
    from multiagents.scheduler.windows import evaluate, prepare
    prepare("Invalid/Zone")
    spec = {"days": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"], "ranges": ["00:00-23:59"], "timezone": "Invalid/Zone"}
    encoded = json.dumps([spec])
    # Evaluation shouldn't silently use local system time. It should either fail
    # cleanly or fall back to the default zone.
    # We test this by expecting a failure or an explicit check.
    
    try:
        res = evaluate(encoded, "UTC", 0)
    except Exception as e:
        # If it crashes, it should be a clean error, not an internal datetime NoneType error or similar.
        # But wait, it doesn't crash! It runs fine and returns naive local time.
        pass
    
    # To prove it's using local naive time, we can patch datetime to see what it does, 
    # but more simply: it should not just succeed silently with a wrong timezone.
    # Let's assert it raises a ValueError or similar.
    import pytest
    with pytest.raises((ValueError, KeyError)):
        evaluate(encoded, "UTC", 0)
