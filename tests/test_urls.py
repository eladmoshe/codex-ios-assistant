import unittest

from iphone_cli.errors import IPhoneError
from iphone_cli.urls import (
    calculator_url,
    calendar_url,
    camera_url,
    doordash_url,
    find_my_url,
    messages_compose_url,
    messages_group_compose_url,
    messages_message_url,
    normalize_open_url,
    parse_duration,
    spotify_url,
    uber_url,
    weather_url,
)


class URLTests(unittest.TestCase):
    def test_camera_defaults_to_plain_launcher(self):
        self.assertEqual(camera_url(), "camera://")

    def test_bare_web_domain_defaults_to_https(self):
        self.assertEqual(normalize_open_url("google.com"), "https://google.com")
        self.assertEqual(
            normalize_open_url("example.com/path?q=hello"),
            "https://example.com/path?q=hello",
        )

    def test_open_url_preserves_explicit_schemes(self):
        self.assertEqual(normalize_open_url("http://example.com"), "http://example.com")
        self.assertEqual(normalize_open_url("camera://"), "camera://")

    def test_open_url_rejects_ambiguous_text(self):
        with self.assertRaises(IPhoneError):
            normalize_open_url("google")
        with self.assertRaises(IPhoneError):
            normalize_open_url("hello world.com")

    def test_front_video_camera(self):
        self.assertEqual(
            camera_url("video", "front"),
            "camera://configuration?capturemode=video&capturedevice=front",
        )

    def test_weather_opens_generically_or_to_coordinates(self):
        self.assertEqual(weather_url(), "weather://")
        self.assertEqual(
            weather_url(latitude=41.8781, longitude=-87.6298),
            "weather://weather.apple.com/?lat=41.8781&long=-87.6298",
        )

    def test_weather_requires_a_valid_coordinate_pair(self):
        with self.assertRaises(IPhoneError):
            weather_url(latitude=41.8781)
        with self.assertRaises(IPhoneError):
            weather_url(latitude=91, longitude=0)
        with self.assertRaises(IPhoneError):
            weather_url(latitude=0, longitude=-181)

    def test_calendar_uses_slashless_apple_epoch_form(self):
        self.assertRegex(calendar_url("2027-01-01"), r"^calshow:\d+\.0$")

    def test_calculator_encodes_plus(self):
        self.assertEqual(calculator_url("40+2"), "calc://40%2B2")

    def test_find_my_preserves_literal_plus(self):
        self.assertEqual(
            find_my_url(phone="+1 (555) 010-1001"),
            "findmy://friend/+15550101001",
        )

    def test_messages_compose_is_unsent_validated_form(self):
        self.assertEqual(
            messages_compose_url("+15550101001", "How are you?"),
            "sms://open?address=%2B15550101001&body=How%20are%20you%3F",
        )

    def test_messages_group_compose_uses_synced_group_id(self):
        group_id = "bf86e595-e17b-4d78-8178-49ee8b53c06c"
        self.assertEqual(
            messages_group_compose_url(group_id, "hello there frens"),
            "sms://open?groupid=BF86E595-E17B-4D78-8178-49EE8B53C06C&"
            "body=hello%20there%20frens",
        )

    def test_messages_group_compose_rejects_non_uuid(self):
        with self.assertRaises(IPhoneError):
            messages_group_compose_url("chat740", "hello")

    def test_message_guid_is_validated(self):
        guid = "3690DA00-7DDA-4B72-A847-197715B89D82"
        self.assertEqual(messages_message_url(guid), f"sms://open?message-guid={guid}")
        with self.assertRaises(IPhoneError):
            messages_message_url("not-a-guid")

    def test_uber_requires_coordinates_and_encodes_labels(self):
        url = uber_url(
            latitude=37.784436,
            longitude=-122.403214,
            name="AMC Metreon 16",
            address="135 4th St, San Francisco, CA 94103",
        )
        self.assertIn("dropoff[latitude]=37.784436", url)
        self.assertIn("dropoff[longitude]=-122.403214", url)
        self.assertIn("AMC%20Metreon%2016", url)

    def test_doordash_rejects_non_store_urls(self):
        good = "https://www.doordash.com/en/store/chipotle-san-francisco-270093/"
        self.assertEqual(doordash_url(good), good)
        with self.assertRaises(IPhoneError):
            doordash_url("https://example.com/store/270093")
        with self.assertRaises(IPhoneError):
            doordash_url("https://www.doordash.com/")

    def test_spotify_accepts_only_spotify_content_urls(self):
        good = "https://open.spotify.com/playlist/37i9dQZF1DXaQm3ZVg9Z2X"
        self.assertEqual(spotify_url(good), good)
        with self.assertRaises(IPhoneError):
            spotify_url("https://example.com/playlist/37i9dQZF1DXaQm3ZVg9Z2X")


class DurationTests(unittest.TestCase):
    def test_duration_forms(self):
        self.assertEqual(parse_duration("90"), 90)
        self.assertEqual(parse_duration("10m"), 600)
        self.assertEqual(parse_duration("1h30m"), 5400)
        self.assertEqual(parse_duration("2.5m"), 150)

    def test_invalid_duration(self):
        for value in ("", "ten minutes", "1m30", "0"):
            with self.subTest(value=value), self.assertRaises(IPhoneError):
                parse_duration(value)


if __name__ == "__main__":
    unittest.main()
