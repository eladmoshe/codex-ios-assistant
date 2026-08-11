"""Pure builders for the deep links understood by the iPhone apps."""

from __future__ import annotations

import re
import time
from datetime import date, datetime, timedelta
from ipaddress import ip_address
from urllib.parse import quote, urlencode, urlsplit

from .errors import IPhoneError


APPLE_EPOCH_UNIX = 978_307_200
GUID_PATTERN = re.compile(
    r"^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-"
    r"[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$"
)
DNS_LABEL_PATTERN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")


def require_url(value: str, *, hosts: tuple[str, ...] = ()) -> str:
    """Validate a URL and optionally restrict it to an exact host or subdomain."""
    value = value.strip()
    parsed = urlsplit(value)
    if not parsed.scheme:
        raise IPhoneError(f"Expected a complete URL, got: {value!r}")

    if hosts:
        host = (parsed.hostname or "").lower()
        allowed = any(host == candidate or host.endswith(f".{candidate}") for candidate in hosts)
        if not allowed:
            expected = " or ".join(hosts)
            raise IPhoneError(f"Expected a URL on {expected}, got host {host or '(none)'!r}.")
    return value


def normalize_open_url(value: str) -> str:
    """Accept a full URL or turn an unambiguous bare web host into HTTPS."""
    value = value.strip()
    if not value:
        raise IPhoneError("URL cannot be empty.")
    if any(character.isspace() or ord(character) < 32 for character in value):
        raise IPhoneError(f"URL contains whitespace or control characters: {value!r}")

    parsed = urlsplit(value)
    if parsed.scheme:
        return value

    candidate = f"https://{value}"
    parsed = urlsplit(candidate)
    host = parsed.hostname
    if not parsed.netloc or not host or parsed.username is not None:
        raise IPhoneError(f"Expected a URL or web domain, got: {value!r}")
    try:
        parsed.port
    except ValueError as error:
        raise IPhoneError(f"Invalid port in URL: {value!r}") from error

    if host.lower() != "localhost":
        try:
            ip_address(host)
        except ValueError:
            try:
                ascii_host = host.encode("idna").decode("ascii")
            except UnicodeError as error:
                raise IPhoneError(f"Invalid web domain: {host!r}") from error
            labels = ascii_host.rstrip(".").split(".")
            if len(labels) < 2 or not all(DNS_LABEL_PATTERN.fullmatch(label) for label in labels):
                raise IPhoneError(f"Expected a URL or web domain, got: {value!r}")
    return candidate


def camera_url(mode: str | None = None, facing: str | None = None) -> str:
    if mode is None and facing is None:
        return "camera://"
    mode = mode or "photo"
    facing = facing or "back"
    if mode not in {"photo", "video"}:
        raise IPhoneError("Camera mode must be 'photo' or 'video'.")
    if facing not in {"front", "back"}:
        raise IPhoneError("Camera facing must be 'front' or 'back'.")
    return f"camera://configuration?capturemode={mode}&capturedevice={facing}"


def weather_url(
    *,
    latitude: float | None = None,
    longitude: float | None = None,
) -> str:
    if latitude is None and longitude is None:
        return "weather://"
    if latitude is None or longitude is None:
        raise IPhoneError("Weather location requires both latitude and longitude.")
    if not -90 <= latitude <= 90:
        raise IPhoneError("Latitude must be between -90 and 90.")
    if not -180 <= longitude <= 180:
        raise IPhoneError("Longitude must be between -180 and 180.")
    formatted_latitude = format(latitude, ".8f").rstrip("0").rstrip(".")
    formatted_longitude = format(longitude, ".8f").rstrip("0").rstrip(".")
    return (
        "weather://weather.apple.com/"
        f"?lat={formatted_latitude}&long={formatted_longitude}"
    )


def parse_calendar_date(value: str) -> date:
    lowered = value.strip().lower()
    today = date.today()
    if lowered == "today":
        return today
    if lowered == "tomorrow":
        return today + timedelta(days=1)
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise IPhoneError("Date must be YYYY-MM-DD, 'today', or 'tomorrow'.") from error


def calendar_url(value: str = "today") -> str:
    target = parse_calendar_date(value)
    local_midnight = datetime.combine(target, datetime.min.time())
    # A naive datetime's timestamp uses the Mac's local timezone, including the
    # historical/future daylight-saving rule for the requested date.
    apple_seconds = int(time.mktime(local_midnight.timetuple())) - APPLE_EPOCH_UNIX
    return f"calshow:{apple_seconds}.0"


def calculator_url(expression: str | None = None) -> str:
    if expression is None:
        return "calc://"
    expression = expression.strip()
    if not expression:
        raise IPhoneError("Calculator expression cannot be empty.")
    return f"calc://{quote(expression, safe='')}"


def normalize_phone(value: str) -> str:
    value = value.strip()
    digits = "".join(character for character in value if character.isdigit())
    if not digits:
        raise IPhoneError(f"No phone number found in {value!r}.")
    if value.startswith("+"):
        return f"+{digits}"
    if value.startswith("00"):
        return f"+{digits[2:]}"
    return digits


def looks_like_phone(value: str) -> bool:
    allowed = set("+0123456789().- ")
    return sum(character.isdigit() for character in value) >= 3 and all(
        character in allowed for character in value
    )


def messages_thread_url(address: str) -> str:
    address = address.strip()
    if not address:
        raise IPhoneError("Message address cannot be empty.")
    return f"sms://open?address={quote(address, safe='')}"


def messages_compose_url(address: str, body: str) -> str:
    address = address.strip()
    if not address:
        raise IPhoneError("Message recipient cannot be empty.")
    if not body:
        raise IPhoneError("Message body cannot be empty.")
    return (
        "sms://open?"
        f"address={quote(address, safe='')}&"
        f"body={quote(body, safe='')}"
    )


def messages_group_compose_url(group_id: str, body: str) -> str:
    group_id = group_id.strip()
    if not GUID_PATTERN.fullmatch(group_id):
        raise IPhoneError(
            "Message group ID must be a UUID such as "
            "XXXXXXXX-XXXX-XXXX-XXXX-XXXXXXXXXXXX."
        )
    if not body:
        raise IPhoneError("Message body cannot be empty.")
    return (
        "sms://open?"
        f"groupid={group_id.upper()}&"
        f"body={quote(body, safe='')}"
    )


def messages_message_url(guid: str) -> str:
    guid = guid.strip()
    if not GUID_PATTERN.fullmatch(guid):
        raise IPhoneError("Message ID must be a UUID such as XXXXXXXX-XXXX-XXXX-XXXX-XXXXXXXXXXXX.")
    return f"sms://open?message-guid={guid.upper()}"


def find_my_url(*, tab: str | None = None, phone: str | None = None) -> str:
    if tab and phone:
        raise IPhoneError("Choose either a Find My tab or a person, not both.")
    if phone:
        # Find My's private parser requires a literal leading plus sign here;
        # percent-encoding the plus was tested and did not select the person.
        return f"findmy://friend/{normalize_phone(phone)}"
    if tab:
        if tab not in {"people", "devices", "items", "me"}:
            raise IPhoneError("Find My tab must be people, devices, items, or me.")
        return f"findmy://{tab}"
    return "findmy://"


def uber_url(
    *,
    latitude: float,
    longitude: float,
    name: str,
    address: str,
) -> str:
    if not -90 <= latitude <= 90:
        raise IPhoneError("Latitude must be between -90 and 90.")
    if not -180 <= longitude <= 180:
        raise IPhoneError("Longitude must be between -180 and 180.")
    parameters = [
        ("pickup", "my_location"),
        ("dropoff[latitude]", format(latitude, ".8f").rstrip("0").rstrip(".")),
        ("dropoff[longitude]", format(longitude, ".8f").rstrip("0").rstrip(".")),
        ("dropoff[nickname]", name),
        ("dropoff[formatted_address]", address),
    ]
    query = urlencode(parameters, quote_via=quote, safe="[]")
    return f"uber://riderequest?{query}"


def doordash_url(value: str) -> str:
    value = require_url(value, hosts=("doordash.com",))
    if "/store/" not in urlsplit(value).path:
        raise IPhoneError("DoorDash URL must be a specific storefront URL containing '/store/'.")
    return value


def spotify_url(value: str) -> str:
    return require_url(value, hosts=("open.spotify.com",))


def books_url(value: str | None = None) -> str:
    return require_url(value or "https://books.apple.com/", hosts=("books.apple.com",))


def app_store_url(value: str) -> str:
    return require_url(value, hosts=("apps.apple.com",))


def parse_duration(value: str) -> int:
    """Parse seconds or a compact duration such as 1h30m or 2.5m."""
    value = value.strip().lower()
    if value.isdigit():
        seconds = int(value)
    else:
        position = 0
        total = 0.0
        for match in re.finditer(r"(\d+(?:\.\d+)?)(h|m|s)", value):
            if match.start() != position:
                raise IPhoneError("Duration must look like 90, 10m, 1h30m, or 2.5m.")
            amount = float(match.group(1))
            multiplier = {"h": 3600, "m": 60, "s": 1}[match.group(2)]
            total += amount * multiplier
            position = match.end()
        if position != len(value) or position == 0:
            raise IPhoneError("Duration must look like 90, 10m, 1h30m, or 2.5m.")
        seconds = round(total)
    if seconds <= 0:
        raise IPhoneError("Duration must be greater than zero.")
    return seconds
