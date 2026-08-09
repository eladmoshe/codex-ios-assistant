"""Argument parsing and command dispatch for the iphone executable."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from typing import Callable

from . import __version__
from .contact_search import run_contact_search
from .errors import IPhoneError
from .message_history import run_message_read
from .protocol import PROTOCOL_VERSION, REQUEST_ID_PATTERN, new_request_id
from .resolve import (
    resolve_contact_phone,
    resolve_message_compose_target,
    resolve_message_recipient,
)
from .transport import Operation, Result, dependency_report, execute
from .urls import (
    app_store_url,
    books_url,
    calculator_url,
    calendar_url,
    camera_url,
    doordash_url,
    find_my_url,
    messages_compose_url,
    messages_group_compose_url,
    messages_message_url,
    messages_thread_url,
    normalize_open_url,
    parse_duration,
    spotify_url,
    uber_url,
    weather_url,
)


Handler = Callable[[argparse.Namespace], Operation | Result]


def _timeout(value: str) -> float:
    try:
        result = float(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("timeout must be a number of seconds") from error
    if result <= 0:
        raise argparse.ArgumentTypeError("timeout must be greater than zero")
    return result


def _positive_int(value: str) -> int:
    try:
        result = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("value must be a positive integer") from error
    if result <= 0:
        raise argparse.ArgumentTypeError("value must be a positive integer")
    return result


def _request_id(value: str) -> str:
    if REQUEST_ID_PATTERN.fullmatch(value) is None:
        raise argparse.ArgumentTypeError(
            "request id must be exactly 32 lowercase hexadecimal characters"
        )
    return value


def _alarm_time(value: str) -> str:
    compact = re.sub(r"\s+", "", value).replace(".", "").upper()
    for pattern in ("%H:%M", "%I:%M%p", "%I%p"):
        try:
            parsed = dt.datetime.strptime(compact, pattern)
        except ValueError:
            continue
        return parsed.strftime("%H:%M")
    raise argparse.ArgumentTypeError(
        "alarm time must be HH:MM or a 12-hour time such as '7:30 AM'"
    )


def _display_alarm_time(value: str) -> str:
    hour_text, minute_text = value.split(":", 1)
    hour = int(hour_text)
    suffix = "AM" if hour < 12 else "PM"
    hour = hour % 12 or 12
    return f"{hour}:{minute_text} {suffix}"


def _global_parent() -> argparse.ArgumentParser:
    parent = argparse.ArgumentParser(add_help=False)
    parent.add_argument(
        "-n",
        "--dry-run",
        action="store_true",
        default=argparse.SUPPRESS,
        help="show the underlying command without contacting the iPhone",
    )
    parent.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        default=argparse.SUPPRESS,
        help="print a stable JSON result",
    )
    parent.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        default=argparse.SUPPRESS,
        help="show implementation details on stderr",
    )
    parent.add_argument(
        "--timeout",
        type=_timeout,
        default=argparse.SUPPRESS,
        metavar="SECONDS",
        help="maximum wait for the iPhone or a helper (default: 30)",
    )
    parent.add_argument(
        "--request-id",
        type=_request_id,
        default=argparse.SUPPRESS,
        metavar="32-HEX-ID",
        help="correlation id supplied by the caller (32 lowercase hex characters)",
    )
    parent.add_argument(
        "--version",
        action="version",
        version=f"iphone {__version__}",
    )
    return parent


def _subparser(
    collection: argparse._SubParsersAction[argparse.ArgumentParser],
    name: str,
    parent: argparse.ArgumentParser,
    **kwargs: object,
) -> argparse.ArgumentParser:
    kwargs.setdefault("formatter_class", argparse.RawDescriptionHelpFormatter)
    return collection.add_parser(name, parents=[parent], **kwargs)


def _action_parser(
    resource_parser: argparse.ArgumentParser,
    parent: argparse.ArgumentParser,
    name: str,
    **kwargs: object,
) -> argparse.ArgumentParser:
    actions = getattr(resource_parser, "_iphone_actions", None)
    if actions is None:
        actions = resource_parser.add_subparsers(dest="action", title="actions")
        setattr(resource_parser, "_iphone_actions", actions)
        resource_parser.set_defaults(_help_parser=resource_parser)
    return _subparser(actions, name, parent, **kwargs)


def _open_operation(resource: str, url: str, summary: str, **metadata: object) -> Operation:
    return Operation(
        resource=resource,
        action="open",
        kind="hola",
        arguments=("openurl", url),
        summary=summary,
        url=url,
        metadata=dict(metadata),
    )


def _read_value(value: str, *, label: str) -> str:
    if value != "-":
        return value
    if sys.stdin.isatty():
        raise IPhoneError(f"{label} was '-', but no input was piped on stdin.")
    return sys.stdin.read()


def _handle_url(args: argparse.Namespace) -> Operation:
    url = normalize_open_url(args.url)
    return _open_operation("url", url, "URL opened.")


def _handle_camera(args: argparse.Namespace) -> Operation:
    url = camera_url(args.mode, args.facing)
    details = [value for value in (args.mode, args.facing) if value]
    suffix = f" ({', '.join(details)})" if details else ""
    return _open_operation("camera", url, f"Camera opened{suffix}.")


def _handle_weather(args: argparse.Namespace) -> Operation:
    if args.location and args.latitude is None and args.longitude is None:
        raise IPhoneError("--location requires --latitude and --longitude.")
    url = weather_url(latitude=args.latitude, longitude=args.longitude)
    if args.latitude is None:
        summary = "Weather opened."
    else:
        label = args.location or f"{args.latitude:g}, {args.longitude:g}"
        summary = f"Weather opened for {label}."
    return _open_operation(
        "weather",
        url,
        summary,
        location=args.location,
        latitude=args.latitude,
        longitude=args.longitude,
    )


def _handle_calendar(args: argparse.Namespace) -> Operation:
    url = calendar_url(args.date)
    return _open_operation(
        "calendar",
        url,
        f"Calendar opened to {args.date}.",
        date=args.date,
    )


def _handle_calculator_open(args: argparse.Namespace) -> Operation:
    return _open_operation("calculator", calculator_url(), "Calculator opened.")


def _handle_calculator_evaluate(args: argparse.Namespace) -> Operation:
    expression = _read_value(args.expression, label="expression").strip()
    url = calculator_url(expression)
    operation = _open_operation(
        "calculator",
        url,
        f"Calculation {expression!r} opened in Calculator.",
        expression=expression,
    )
    return Operation(**{**operation.__dict__, "action": "evaluate"})


def _handle_messages_open(args: argparse.Namespace) -> Operation:
    if args.message:
        url = messages_message_url(args.message)
        summary = "Specific message opened in Messages."
        metadata = {"message": args.message.upper()}
    elif args.address:
        url = messages_thread_url(args.address)
        summary = f"Messages conversation with {args.address} opened."
        metadata = {"address": args.address}
    elif args.thread:
        address = resolve_message_recipient(args.thread)
        url = messages_thread_url(address)
        summary = f"Messages conversation with {args.thread} opened."
        metadata = {"thread": args.thread, "address": address}
    else:
        url = "ichat://"
        summary = "Messages opened."
        metadata = {}
    return _open_operation("messages", url, summary, **metadata)


def _handle_contacts_search(args: argparse.Namespace) -> Result:
    return run_contact_search(
        args.query,
        dry_run=getattr(args, "dry_run", False),
        json_output=getattr(args, "json_output", False),
        timeout=getattr(args, "timeout", 30),
    )


def _handle_messages_compose(args: argparse.Namespace) -> Operation:
    target = resolve_message_compose_target(args.to)
    body = _read_value(args.body, label="message body")
    if target.is_group:
        assert target.group_id is not None
        url = messages_group_compose_url(target.group_id, body)
        summary = f"Message drafted in {target.label}."
        metadata = {
            "recipient": args.to,
            "thread": target.label,
            "group_id": target.group_id,
            "body": body,
        }
    else:
        assert target.address is not None
        url = messages_compose_url(target.address, body)
        summary = f"Message drafted to {args.to}."
        metadata = {
            "recipient": args.to,
            "address": target.address,
            "body": body,
        }
    return Operation(
        resource="messages",
        action="compose",
        kind="hola",
        arguments=("openurl", url),
        summary=summary,
        url=url,
        metadata=metadata,
    )


def _run_messages_read(args: argparse.Namespace, action: str, arguments: list[str]) -> Result:
    return run_message_read(
        action,
        arguments,
        dry_run=getattr(args, "dry_run", False),
        json_output=getattr(args, "json_output", False),
        timeout=getattr(args, "timeout", 30),
    )


def _handle_messages_chats(args: argparse.Namespace) -> Result:
    arguments = ["--limit", str(args.limit)]
    if args.unread_only:
        arguments.append("--unread-only")
    return _run_messages_read(args, "chats", arguments)


def _handle_messages_history(args: argparse.Namespace) -> Result:
    arguments = ["--chat-id", str(args.chat_id), "--limit", str(args.limit)]
    for flag, value in (("--participants", args.participants), ("--start", args.start), ("--end", args.end)):
        if value:
            arguments.extend((flag, value))
    if args.attachments:
        arguments.append("--attachments")
    return _run_messages_read(args, "history", arguments)


def _handle_messages_search(args: argparse.Namespace) -> Result:
    return _run_messages_read(
        args,
        "search",
        ["--query", args.query, "--match", args.match, "--limit", str(args.limit)],
    )


def _handle_messages_group(args: argparse.Namespace) -> Result:
    return _run_messages_read(args, "group", ["--chat-id", str(args.chat_id)])


def _handle_find_my(args: argparse.Namespace) -> Operation:
    phone: str | None = None
    if args.person:
        phone = resolve_contact_phone(args.person)
    elif args.phone:
        phone = args.phone
    url = find_my_url(tab=args.tab, phone=phone)
    if args.person:
        summary = f"{args.person} opened in Find My."
    elif phone:
        summary = f"{phone} opened in Find My."
    elif args.tab:
        summary = f"Find My opened to the {args.tab} tab."
    else:
        summary = "Find My opened."
    return _open_operation(
        "find-my",
        url,
        summary,
        person=args.person,
        phone=phone,
        tab=args.tab,
    )


def _handle_uber(args: argparse.Namespace) -> Operation:
    latitude = args.latitude
    longitude = args.longitude
    name = args.name or args.destination
    address = args.address or args.destination

    url = uber_url(
        latitude=latitude,
        longitude=longitude,
        name=name,
        address=address,
    )
    summary = f"Uber opened with {name} as the destination."
    return _open_operation(
        "uber",
        url,
        summary,
        destination=args.destination,
        name=name,
        address=address,
        latitude=latitude,
        longitude=longitude,
    )


def _handle_doordash(args: argparse.Namespace) -> Operation:
    url = doordash_url(args.store_url)
    return _open_operation(
        "doordash",
        url,
        "DoorDash storefront opened.",
    )


def _handle_spotify(args: argparse.Namespace) -> Operation:
    url = spotify_url(args.url)
    return _open_operation(
        "spotify",
        url,
        "Spotify content opened.",
    )


def _handle_fixed_url(resource: str, url: str, summary: str) -> Handler:
    def handler(args: argparse.Namespace) -> Operation:
        return _open_operation(resource, url, summary)

    return handler


def _handle_books(args: argparse.Namespace) -> Operation:
    return _open_operation("books", books_url(args.url), "Books opened.")


def _handle_app_store(args: argparse.Namespace) -> Operation:
    return _open_operation(
        "app-store",
        app_store_url(args.url),
        "App Store product page opened.",
    )


def _handle_screen_read(args: argparse.Namespace) -> Operation:
    return Operation(resource="screen", action="read", kind="screen-read")


def _handle_screen_capture(args: argparse.Namespace) -> Operation:
    return Operation(resource="screen", action="capture", kind="screen-capture")


def _handle_home(args: argparse.Namespace) -> Operation:
    return Operation(
        resource="home",
        action="open",
        kind="hola",
        arguments=("homescreen",),
        summary="Home Screen shown.",
        receipt_action="home.open",
    )


def _handle_flashlight(args: argparse.Namespace) -> Operation:
    action = "turned on" if args.state == "on" else "turned off"
    return Operation(
        resource="flashlight",
        action=args.state,
        kind="hola",
        arguments=("flashlight", args.state),
        summary=f"Flashlight {action}.",
        receipt_action="flashlight.set",
    )


def _handle_timer_start(args: argparse.Namespace) -> Operation:
    seconds = parse_duration(args.duration)
    return Operation(
        resource="timer",
        action="start",
        kind="hola",
        arguments=("timer", "start", str(seconds)),
        summary=f"Timer started for {seconds} seconds.",
        metadata={"seconds": seconds},
    )


def _handle_timer_control(action: str) -> Handler:
    def handler(args: argparse.Namespace) -> Operation:
        past_tense = {"pause": "paused", "resume": "resumed", "cancel": "canceled"}[action]
        return Operation(
            resource="timer",
            action=action,
            kind="hola",
            arguments=("timer", action),
            summary=f"Timer {past_tense}.",
        )

    return handler


def _handle_alarm_list(args: argparse.Namespace) -> Operation:
    return Operation(resource="alarm", action="list", kind="alarm-read")


def _handle_alarm_set(args: argparse.Namespace) -> Operation:
    label = args.label.strip()
    if not label:
        raise IPhoneError("--label cannot be empty.")
    return Operation(
        resource="alarm",
        action="set",
        kind="hola",
        arguments=("alarm", "set", args.time, label),
        summary=f"Alarm requested for {_display_alarm_time(args.time)}.",
        metadata={"time": args.time, "label": label},
    )


def _handle_alarm_off(args: argparse.Namespace) -> Operation:
    return Operation(
        resource="alarm",
        action="disable_at",
        kind="hola",
        arguments=("alarm", "off", args.time),
        summary=f"Alarm-off requested for {_display_alarm_time(args.time)}.",
        metadata={"time": args.time},
    )


def _handle_call(args: argparse.Namespace) -> Operation:
    phone = resolve_contact_phone(args.recipient)
    return Operation(
        resource="call",
        action="start",
        kind="hola",
        arguments=("call", phone),
        summary=f"Call placed to {args.recipient}.",
        metadata={"recipient": args.recipient, "phone": phone},
    )


def _handle_clipboard_copy(args: argparse.Namespace) -> Operation:
    text = _read_value(args.text, label="clipboard text")
    return Operation(
        resource="clipboard",
        action="copy",
        kind="hola",
        arguments=("copytoclipboard", text),
        summary="Clipboard updated.",
        metadata={"text": text},
    )


def _handle_clipboard_get(args: argparse.Namespace) -> Operation:
    return Operation(resource="clipboard", action="get", kind="clipboard-read")


def _handle_low_power(args: argparse.Namespace) -> Operation:
    action = "turned on" if args.state == "on" else "turned off"
    return Operation(
        resource="low-power",
        action=args.state,
        kind="hola",
        arguments=("lowpower", args.state),
        summary=f"Low Power Mode {action}.",
        receipt_action="low_power.set",
    )


def _handle_control_center(args: argparse.Namespace) -> Operation:
    action = "opened" if args.state == "open" else "closed"
    return Operation(
        resource="control-center",
        action=args.state,
        kind="hola",
        arguments=("controlcenter", args.state),
        summary=f"Control Center {action}.",
        receipt_action="control_center.set",
    )


def _handle_doctor(args: argparse.Namespace) -> Result:
    checks = dependency_report()
    healthy = all(check["available"] for check in checks if check["required"])
    return Result(
        resource="doctor",
        action="check",
        status="completed" if healthy else "failed",
        summary="Dependencies checked." if healthy else "Dependency check failed.",
        data={"checks": checks},
    )


def build_parser() -> argparse.ArgumentParser:
    global_parent = _global_parent()
    parser = argparse.ArgumentParser(
        prog="iphone",
        parents=[global_parent],
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Control an iPhone and inspect related Mac data through a semantic CLI.",
        epilog="""examples:
  iphone camera open --mode video --facing front
  iphone weather open --location Chicago --lat 41.8781 --lng -87.6298
  iphone contacts search Johnny
  iphone messages open --thread 'Jane Appleseed'
  iphone messages compose --to 'Jane Appleseed' --body 'How are you?'
  iphone uber open --destination 'AMC Metreon' --lat 37.784436 --lng -122.403214
  iphone spotify open 'https://open.spotify.com/track/...'
  iphone timer start 10m
  iphone alarm set '7:30 AM' --label 'Wake up'
  iphone alarm list
  iphone alarm off '7:30 AM'
  iphone screen read
  iphone --dry-run calendar open --date 2027-01-01
""",
    )
    parser.set_defaults(_help_parser=parser)
    resources = parser.add_subparsers(dest="resource", title="resources")

    url_parser = _subparser(
        resources,
        "url",
        global_parent,
        help="open an arbitrary URL",
        description="Open a complete URL or a bare web domain on the iPhone.",
        epilog="example:\n  iphone url open google.com",
    )
    url_open = _action_parser(
        url_parser,
        global_parent,
        "open",
        help="open a URL or bare web domain (defaults to HTTPS)",
    )
    url_open.add_argument("url")
    url_open.set_defaults(handler=_handle_url)

    camera = _subparser(
        resources,
        "camera",
        global_parent,
        help="open Camera in a chosen mode",
        description="Open Camera normally or select the capture mode and lens.",
        epilog="examples:\n  iphone camera open\n  iphone camera open --mode video --facing front",
    )
    camera_open = _action_parser(camera, global_parent, "open", help="open Camera")
    camera_open.add_argument("--mode", choices=("photo", "video"))
    camera_open.add_argument("--facing", choices=("front", "back"))
    camera_open.set_defaults(handler=_handle_camera)

    weather = _subparser(
        resources,
        "weather",
        global_parent,
        help="open Weather generally or to a location",
        description=(
            "Open Weather normally or show the forecast at verified coordinates.\n"
            "The optional location name is a display label; coordinates choose the forecast."
        ),
        epilog=(
            "examples:\n"
            "  iphone weather open\n"
            "  iphone weather open --location Chicago --lat 41.8781 --lng -87.6298"
        ),
    )
    weather_open = _action_parser(
        weather,
        global_parent,
        "open",
        help="open Weather generally or to coordinates",
    )
    weather_open.add_argument("--location", help="descriptive location name")
    weather_open.add_argument("--latitude", "--lat", type=float)
    weather_open.add_argument("--longitude", "--lng", "--lon", type=float)
    weather_open.set_defaults(handler=_handle_weather)

    calendar = _subparser(
        resources,
        "calendar",
        global_parent,
        help="open Calendar to a date",
        description="Open Calendar to today, tomorrow, or a specific date.",
        epilog="examples:\n  iphone calendar open --date today\n  iphone calendar open --date 2027-01-01",
    )
    calendar_open = _action_parser(calendar, global_parent, "open", help="open Calendar to a date")
    calendar_open.add_argument("--date", default="today", metavar="YYYY-MM-DD")
    calendar_open.set_defaults(handler=_handle_calendar)

    calculator = _subparser(
        resources,
        "calculator",
        global_parent,
        help="open or evaluate in Calculator",
        description="Open Calculator or prefill it with an expression.",
        epilog="examples:\n  iphone calculator open\n  iphone calculator evaluate '40+2'",
    )
    calculator_open = _action_parser(calculator, global_parent, "open", help="open Calculator")
    calculator_open.set_defaults(handler=_handle_calculator_open)
    calculator_evaluate = _action_parser(
        calculator,
        global_parent,
        "evaluate",
        help="open Calculator with an expression",
    )
    calculator_evaluate.add_argument("expression", help="expression, or - to read stdin")
    calculator_evaluate.set_defaults(handler=_handle_calculator_evaluate)

    contacts = _subparser(
        resources,
        "contacts",
        global_parent,
        help="search Mac Contacts",
        description=(
            "Search Mac Contacts by name, organization, email, or phone number.\n"
            "This command is local and read-only."
        ),
        epilog=(
            "examples:\n"
            "  iphone contacts search 'Johnny'\n"
            "  iphone contacts search 'johnny@example.com' --json"
        ),
    )
    contacts_search = _action_parser(
        contacts,
        global_parent,
        "search",
        help="find matching contacts",
        description=(
            "Search names, organizations, email addresses, and phone numbers.\n"
            "Returns at most 50 matches and never changes Contacts."
        ),
    )
    contacts_search.add_argument("query", help="name, organization, email, or phone number")
    contacts_search.set_defaults(handler=_handle_contacts_search)

    messages = _subparser(
        resources,
        "messages",
        global_parent,
        help="open, draft, or read Messages",
        description=(
            "Open Messages, compose an unsent draft, or read the local Messages database.\n"
            "History actions are finite and read-only. The compose action never sends the message."
        ),
        epilog=(
            "examples:\n"
            "  iphone messages open\n"
            "  iphone messages open --thread 'Jane Appleseed'\n"
            "  iphone messages compose --to 'Jane Appleseed' --body 'How are you?'\n"
            "  iphone messages compose --to 'best boys' --body 'hello there frens'\n"
            "  iphone messages chats --limit 20\n"
            "  iphone messages history --chat-id 42 --limit 50\n"
            "  iphone messages search 'pizza tonight'"
        ),
    )
    messages_open = _action_parser(
        messages,
        global_parent,
        "open",
        help="open Messages, a conversation, or a specific message",
        description=(
            "With no target, open the main Messages screen. Otherwise choose exactly one of\n"
            "--thread, --address, or --message."
        ),
        epilog=(
            "examples:\n"
            "  iphone messages open\n"
            "  iphone messages open --thread 'Jane Appleseed'\n"
            "  iphone messages open --address +15550101001"
        ),
    )
    messages_target = messages_open.add_mutually_exclusive_group()
    messages_target.add_argument("--thread", help="recent conversation name")
    messages_target.add_argument("--address", help="phone number or email address")
    messages_target.add_argument("--message", help="specific message GUID")
    messages_open.set_defaults(handler=_handle_messages_open)
    messages_compose = _action_parser(
        messages,
        global_parent,
        "compose",
        help="open an unsent message draft",
        description=(
            "Open an unsent draft in a direct conversation or existing named group.\n"
            "For a group, --to is its existing Messages name; exact capitalization wins\n"
            "when similarly named groups exist. This command never presses Send."
        ),
        epilog=(
            "examples:\n"
            "  iphone messages compose --to 'Jane Appleseed' --body 'How are you?'\n"
            "  iphone messages compose --to 'best boys' --body 'hello there frens'"
        ),
    )
    messages_compose.add_argument(
        "--to",
        required=True,
        help="contact, existing group name, phone number, or email",
    )
    messages_compose.add_argument("--body", required=True, help="unsent body, or - to read stdin")
    messages_compose.set_defaults(handler=_handle_messages_compose)

    messages_chats = _action_parser(
        messages,
        global_parent,
        "chats",
        help="list recent chats from the Mac Messages database",
        description="List recent conversations and their chat IDs without changing Messages.",
        epilog="examples:\n  iphone messages chats --limit 20\n  iphone messages chats --unread-only --json",
    )
    messages_chats.add_argument("--limit", type=_positive_int, default=20, help="maximum chats (default: 20)")
    messages_chats.add_argument("--unread-only", action="store_true", help="show only chats with unread messages")
    messages_chats.set_defaults(handler=_handle_messages_chats)

    messages_history = _action_parser(
        messages,
        global_parent,
        "history",
        help="read messages from one chat",
        description="Read one chat with the newest messages shown first and optional date, sender, and attachment filters.",
        epilog=(
            "examples:\n"
            "  iphone messages history --chat-id 42 --limit 50\n"
            "  iphone messages history --chat-id 42 --start 2026-01-01T00:00:00Z --json"
        ),
    )
    messages_history.add_argument("--chat-id", type=_positive_int, required=True, help="chat ID from 'iphone messages chats'")
    messages_history.add_argument("--limit", type=_positive_int, default=50, help="maximum messages (default: 50)")
    messages_history.add_argument("--participants", help="comma-separated sender phone numbers or emails")
    messages_history.add_argument("--start", help="inclusive ISO 8601 start time")
    messages_history.add_argument("--end", help="exclusive ISO 8601 end time")
    messages_history.add_argument("--attachments", action="store_true", help="include attachment metadata")
    messages_history.set_defaults(handler=_handle_messages_history)

    messages_search = _action_parser(
        messages,
        global_parent,
        "search",
        help="search text across old messages",
        description="Search message text across the local Messages database.",
        epilog="examples:\n  iphone messages search 'pizza tonight'\n  iphone messages search 'exact phrase' --match exact --json",
    )
    messages_search.add_argument("query", help="message text to find")
    messages_search.add_argument("--match", choices=("contains", "exact"), default="contains")
    messages_search.add_argument("--limit", type=_positive_int, default=50, help="maximum matches (default: 50)")
    messages_search.set_defaults(handler=_handle_messages_search)

    messages_group = _action_parser(
        messages,
        global_parent,
        "group",
        help="inspect a chat and its participants",
        description="Show identifiers, service, and participants for a direct or group chat.",
    )
    messages_group.add_argument("--chat-id", type=_positive_int, required=True, help="chat ID from 'iphone messages chats'")
    messages_group.set_defaults(handler=_handle_messages_group)

    find_my = _subparser(
        resources,
        "find-my",
        global_parent,
        help="open Find My",
        description="Open Find My generally, to a tab, or to a person.",
        epilog="examples:\n  iphone find-my open --tab people\n  iphone find-my open --person 'Jane Appleseed'",
    )
    find_my_open = _action_parser(find_my, global_parent, "open", help="open Find My")
    find_target = find_my_open.add_mutually_exclusive_group()
    find_target.add_argument("--person", help="contact name")
    find_target.add_argument("--phone", help="phone number shared in Find My")
    find_target.add_argument("--tab", choices=("people", "devices", "items", "me"))
    find_my_open.set_defaults(handler=_handle_find_my)

    uber = _subparser(
        resources,
        "uber",
        global_parent,
        help="open a prefilled Uber ride",
        description="Open Uber with a destination prefilled from verified coordinates.",
        epilog=(
            "example:\n"
            "  iphone uber open --destination 'AMC Metreon' --lat 37.784436 --lng -122.403214"
        ),
    )
    uber_open = _action_parser(uber, global_parent, "open", help="open Uber with a destination")
    uber_open.add_argument("--destination", "--to", dest="destination", required=True)
    uber_open.add_argument("--latitude", "--lat", type=float, required=True)
    uber_open.add_argument("--longitude", "--lng", "--lon", type=float, required=True)
    uber_open.add_argument("--name", help="display name shown in Uber")
    uber_open.add_argument("--address", help="formatted address shown in Uber")
    uber_open.set_defaults(handler=_handle_uber)

    doordash = _subparser(
        resources,
        "doordash",
        global_parent,
        help="open a DoorDash storefront",
        description="Open the exact DoorDash branch identified by its public storefront URL.",
    )
    doordash_open = _action_parser(
        doordash,
        global_parent,
        "open",
        help="open an exact restaurant storefront",
    )
    doordash_open.add_argument("--store-url", required=True)
    doordash_open.set_defaults(handler=_handle_doordash)

    spotify = _subparser(
        resources,
        "spotify",
        global_parent,
        help="open Spotify content",
        description="Open a public Spotify track, album, artist, or playlist URL.",
        epilog="example:\n  iphone spotify open 'https://open.spotify.com/track/...'",
    )
    spotify_open = _action_parser(spotify, global_parent, "open", help="open a Spotify content URL")
    spotify_open.add_argument("url", help="open.spotify.com URL")
    spotify_open.set_defaults(handler=_handle_spotify)

    books = _subparser(
        resources,
        "books",
        global_parent,
        help="open Apple Books",
        description="Open Apple Books home or a specific books.apple.com page.",
    )
    books_open = _action_parser(books, global_parent, "open", help="open Books or a book page")
    books_open.add_argument("--url", help="books.apple.com page; defaults to Books home")
    books_open.set_defaults(handler=_handle_books)

    app_store = _subparser(
        resources,
        "app-store",
        global_parent,
        help="open an App Store product page",
        description="Open an apps.apple.com product page in the App Store.",
    )
    app_store_open = _action_parser(app_store, global_parent, "open", help="open an app product page")
    app_store_open.add_argument("--url", required=True, help="apps.apple.com product URL")
    app_store_open.set_defaults(handler=_handle_app_store)

    for name, help_text, url, summary in (
        ("photos", "open Photos", "photos-redirect://a", "Photos opened."),
        ("wallet", "open Wallet", "wallet://", "Wallet opened."),
        ("notes", "open Notes in Quick Note", "mobilenotes://quicknote", "Notes opened."),
    ):
        description = f"{help_text[0].upper()}{help_text[1:]}."
        resource = _subparser(resources, name, global_parent, help=help_text, description=description)
        action = _action_parser(resource, global_parent, "open", help=help_text)
        action.set_defaults(handler=_handle_fixed_url(name, url, summary))

    screen = _subparser(
        resources,
        "screen",
        global_parent,
        help="read or capture the iPhone screen",
        description="Use 'iphone screen read' for visible text, or 'iphone screen capture' for a screenshot.",
        epilog="examples:\n  iphone screen read\n  iphone screen capture --output ~/Desktop/iphone.png",
    )
    screen_read = _action_parser(screen, global_parent, "read", help="return visible screen text")
    screen_read.set_defaults(handler=_handle_screen_read)
    screen_capture = _action_parser(screen, global_parent, "capture", help="save a screenshot")
    screen_capture.add_argument("-o", "--output", help="copy screenshot to this path")
    screen_capture.set_defaults(handler=_handle_screen_capture)

    home = _subparser(resources, "home", global_parent, help="show the iPhone Home Screen")
    home.set_defaults(handler=_handle_home)

    flashlight = _subparser(resources, "flashlight", global_parent, help="control the flashlight")
    flashlight.add_argument("state", choices=("on", "off"))
    flashlight.set_defaults(handler=_handle_flashlight)

    timer = _subparser(
        resources,
        "timer",
        global_parent,
        help="control the active timer",
        description="Start a timer or control the currently active timer.",
        epilog=(
            "examples:\n"
            "  iphone timer start 10m\n"
            "  iphone timer pause\n"
            "  iphone timer resume\n"
            "  iphone timer cancel"
        ),
    )
    timer_start = _action_parser(timer, global_parent, "start", help="start a new timer")
    timer_start.add_argument("duration", help="seconds or a duration such as 10m or 1h30m")
    timer_start.set_defaults(handler=_handle_timer_start)
    for action_name, help_text in (
        ("pause", "pause the active timer"),
        ("resume", "resume the active timer"),
        ("cancel", "cancel the active timer"),
    ):
        timer_action = _action_parser(timer, global_parent, action_name, help=help_text)
        timer_action.set_defaults(handler=_handle_timer_control(action_name))

    alarm = _subparser(
        resources,
        "alarm",
        global_parent,
        help="list, create, or turn off alarms",
        description="Read enabled iPhone alarms, create one, or turn off alarms by time.",
        epilog=(
            "examples:\n"
            "  iphone alarm list\n"
            "  iphone alarm set 07:30 --label 'Wake up'\n"
            "  iphone alarm set '10:30 PM'\n"
            "  iphone alarm off '7:30 AM'"
        ),
    )
    alarm_list = _action_parser(
        alarm,
        global_parent,
        "list",
        help="return enabled alarms from the iPhone",
    )
    alarm_list.set_defaults(handler=_handle_alarm_list)
    alarm_set = _action_parser(
        alarm,
        global_parent,
        "set",
        help="create a new enabled alarm",
    )
    alarm_set.add_argument(
        "time",
        type=_alarm_time,
        help="HH:MM or a quoted 12-hour time such as '7:30 AM'",
    )
    alarm_set.add_argument("--label", default="Alarm", help="alarm label (default: Alarm)")
    alarm_set.set_defaults(handler=_handle_alarm_set)
    alarm_off = _action_parser(
        alarm,
        global_parent,
        "off",
        help="turn off every enabled alarm at an exact time",
    )
    alarm_off.add_argument(
        "time",
        type=_alarm_time,
        help="HH:MM or a quoted 12-hour time; every enabled match is turned off",
    )
    alarm_off.set_defaults(handler=_handle_alarm_off)

    call = _subparser(resources, "call", global_parent, help="place a phone call")
    call.add_argument("recipient", help="contact name or phone number")
    call.set_defaults(handler=_handle_call)

    clipboard = _subparser(
        resources,
        "clipboard",
        global_parent,
        help="read or replace the iPhone clipboard",
        description="Read the current iPhone clipboard or replace it with text.",
        epilog="examples:\n  iphone clipboard get\n  iphone clipboard copy 'Some text'",
    )
    clipboard_copy = _action_parser(clipboard, global_parent, "copy", help="replace clipboard text")
    clipboard_copy.add_argument("text", help="text, or - to read stdin")
    clipboard_copy.set_defaults(handler=_handle_clipboard_copy)
    clipboard_get = _action_parser(clipboard, global_parent, "get", help="return clipboard text")
    clipboard_get.set_defaults(handler=_handle_clipboard_get)

    low_power = _subparser(resources, "low-power", global_parent, help="control Low Power Mode")
    low_power.add_argument("state", choices=("on", "off"))
    low_power.set_defaults(handler=_handle_low_power)

    control_center = _subparser(resources, "control-center", global_parent, help="open or close Control Center")
    control_center.add_argument("state", choices=("open", "close"))
    control_center.set_defaults(handler=_handle_control_center)

    doctor = _subparser(resources, "doctor", global_parent, help="check local dependencies")
    doctor.set_defaults(handler=_handle_doctor)

    return parser


def _failure_action(args: argparse.Namespace) -> tuple[str, str, str]:
    """Derive the policy-owned action identity when a handler fails early."""

    resource = str(getattr(args, "resource", "iphone"))
    leaf_action = getattr(args, "action", None)
    if resource == "call":
        return resource, "start", "call.start"
    if resource == "low-power":
        return resource, str(getattr(args, "state", "set")), "low_power.set"
    if resource == "control-center":
        return resource, str(getattr(args, "state", "set")), "control_center.set"
    if resource == "alarm" and leaf_action == "off":
        return resource, "disable_at", "alarm.disable_at"
    if resource == "home":
        return resource, "open", "home.open"
    if resource == "doctor":
        return resource, "check", "doctor.check"
    action = str(leaf_action or "unknown")
    return resource, action, f"{resource}.{action}"


def _result_dictionary(result: Result, *, requested_request_id: str | None = None) -> dict[str, object]:
    request_id = requested_request_id or result.data.get("request_id")
    if not isinstance(request_id, str) or REQUEST_ID_PATTERN.fullmatch(request_id) is None:
        # Mac-local reads (Contacts/Messages) do not use the phone receiver,
        # but Nami still gets a valid correlation for its own audit record.
        request_id = new_request_id()
    payload = {
        **result.data,
        "ok": result.status in {"completed", "dry-run"},
        # This is a public, versioned contract even for Mac-local reads that
        # do not traverse the phone receiver. Keep it after result.data so a
        # helper cannot spoof or downgrade the protocol version.
        "protocol_version": PROTOCOL_VERSION,
        "status": result.status,
        "resource": result.resource,
        "action": result.action,
        "receipt_action": result.receipt_action or result.resource + "." + result.action,
        "request_id": request_id,
        "summary": result.summary,
    }
    return payload


def _emit(result: Result, args: argparse.Namespace) -> None:
    if getattr(args, "json_output", False):
        print(
            json.dumps(
                _result_dictionary(
                    result,
                    requested_request_id=getattr(args, "request_id", None),
                ),
                indent=2,
                sort_keys=True,
            )
        )
        return

    if result.resource == "doctor":
        for check in result.data["checks"]:
            status = "ok" if check["available"] else "missing"
            qualifier = "required" if check["required"] else "optional"
            path = check["path"] or check["command"]
            print(f"{status:7} {check['name']:18} {path} ({qualifier})")
        print(result.summary)
        return

    if result.resource == "clipboard" and result.action == "get" and result.status == "completed":
        sys.stdout.write(f"<clipboard-contents>{result.summary}</clipboard-contents>")
    else:
        print(result.summary)
    if getattr(args, "verbose", False):
        if url := result.data.get("url"):
            print(f"URL: {url}", file=sys.stderr)
        print(f"Status: {result.status}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not hasattr(args, "handler"):
        args._help_parser.print_help()
        return 0
    try:
        outcome = args.handler(args)
        if isinstance(outcome, Result):
            result = outcome
        else:
            result = execute(
                outcome,
                dry_run=getattr(args, "dry_run", False),
                timeout=getattr(args, "timeout", 30),
                output=getattr(args, "output", None),
                request_id=getattr(args, "request_id", None),
            )
        _emit(result, args)
        # Structured callers (including Nami) must be able to inspect a
        # truthful failed/timeout receipt rather than losing it to a generic
        # subprocess-exit error. Human-readable invocations retain a useful
        # non-zero status for terminal failures.
        if getattr(args, "json_output", False):
            return 0
        return 1 if result.status in {"failed", "timeout"} else 0
    except IPhoneError as error:
        if getattr(args, "json_output", False):
            resource, action, receipt_action = _failure_action(args)
            request_id = getattr(args, "request_id", None) or new_request_id()
            print(
                json.dumps(
                    {
                        "ok": False,
                        "protocol_version": PROTOCOL_VERSION,
                        "status": "failed",
                        "resource": resource,
                        "action": action,
                        "receipt_action": receipt_action,
                        "request_id": request_id,
                        "error_code": "cli_error",
                        "error": str(error),
                        "summary": f"The {receipt_action} operation failed.",
                    },
                    indent=2,
                    sort_keys=True,
                ),
                file=sys.stdout,
            )
            return 0
        else:
            print(f"iphone: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("iphone: interrupted", file=sys.stderr)
        return 130
    except BrokenPipeError:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
