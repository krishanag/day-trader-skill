import datetime
import hashlib
import io
import json
import os
import stat
import tempfile
import unittest
import urllib.error

from _load import load

login = load("trader-kite", "login")
kite = login.kite   # the module login.py calls, so the network fakes reach it

IST = kite.IST


def ist(text):
    return datetime.datetime.fromisoformat(text).replace(tzinfo=IST)


class FakeResponse(io.BytesIO):
    def __init__(self, body, ctype="application/json"):
        super().__init__(body.encode())
        self.headers = {"Content-Type": ctype}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeNet:
    """Records requests and answers them from a queue."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.requests = []

    def __call__(self, req, timeout=None):
        self.requests.append(req)
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


def ok(data):
    return FakeResponse(json.dumps({"status": "success", "data": data}))


def http_error(code, etype, message):
    body = io.BytesIO(json.dumps({"status": "error", "error_type": etype,
                                  "message": message}).encode())
    return urllib.error.HTTPError("https://api.kite.trade/x", code, message, {}, body)


INSTRUMENTS = (
    "instrument_token,exchange_token,tradingsymbol,name,last_price,expiry,strike,tick_size,"
    "lot_size,instrument_type,segment,exchange\n"
    "738561,2885,RELIANCE,RELIANCE INDUSTRIES,0,,0,0.1,1,EQ,NSE,NSE\n"
    "408065,1594,INFY,INFOSYS,0,,0,0.1,1,EQ,NSE,NSE\n"
    "256265,1001,NIFTY 50,NIFTY 50,0,,0,0,0,EQ,INDICES,NSE\n")


class Base(unittest.TestCase):
    def setUp(self):
        self.net = FakeNet()
        self.saved = (kite._urlopen, kite._sleep, dict(kite._last_call))
        kite._urlopen = self.net
        kite._sleep = lambda s: None
        kite._last_call.clear()

    def tearDown(self):
        kite._urlopen, kite._sleep, last = self.saved
        kite._last_call.clear()
        kite._last_call.update(last)


class TokenFreshness(unittest.TestCase):
    def test_token_from_this_morning_is_fresh_all_day(self):
        self.assertTrue(kite.token_is_fresh(ist("2026-10-29T08:50"), ist("2026-10-29T15:29")))

    def test_token_from_yesterday_evening_is_fresh_before_six(self):
        self.assertTrue(kite.token_is_fresh(ist("2026-10-28T20:00"), ist("2026-10-29T05:59")))

    def test_token_from_yesterday_is_stale_after_six(self):
        self.assertFalse(kite.token_is_fresh(ist("2026-10-28T20:00"), ist("2026-10-29T06:00")))

    def test_freshness_compares_in_ist_whatever_the_input_zone(self):
        utc = datetime.timezone.utc
        created = datetime.datetime(2026, 10, 29, 0, 31, tzinfo=utc)   # 06:01 IST
        self.assertTrue(kite.token_is_fresh(created, ist("2026-10-29T12:00")))


class Credentials(unittest.TestCase):
    def test_parse_env_skips_comments_and_strips_quotes_and_export(self):
        text = "# c\n\nexport KITE_API_KEY='abc'\nKITE_API_SECRET=\"s3\"\nJUNK\n"
        self.assertEqual(kite.parse_env(text), {"KITE_API_KEY": "abc", "KITE_API_SECRET": "s3"})

    def test_environment_wins_over_env_file(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, ".env")
            with open(path, "w") as fh:
                fh.write("KITE_API_KEY=file\nKITE_API_SECRET=fs\n")
            creds = kite.credentials({"KITE_API_KEY": "env"}, path)
        self.assertEqual((creds["api_key"], creds["api_secret"]), ("env", "fs"))

    def test_missing_api_key_is_a_clear_error(self):
        with self.assertRaisesRegex(kite.KiteError, "KITE_API_KEY"):
            kite.credentials({}, "/nonexistent/.env")

    def test_checksum_is_sha256_of_key_token_secret(self):
        want = hashlib.sha256(b"keytoksec").hexdigest()
        self.assertEqual(kite.checksum("key", "tok", "sec"), want)


class Session(unittest.TestCase):
    def test_saved_session_is_private_and_reloads_the_same_day(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "trader", "kite_session.json")
            kite.save_session({"access_token": "t0k", "user_id": "AB1234"},
                              ist("2026-10-29T08:55"), path)
            mode = stat.S_IMODE(os.stat(path).st_mode)
            rec = kite.load_session(ist("2026-10-29T14:00"), path)
        self.assertEqual(mode, 0o600)
        self.assertEqual((rec["access_token"], rec["user_id"]), ("t0k", "AB1234"))

    def test_session_from_yesterday_asks_for_login(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "kite_session.json")
            kite.save_session({"access_token": "t0k"}, ist("2026-10-28T09:00"), path)
            with self.assertRaisesRegex(kite.LoginNeeded, "expired at 06:00 IST"):
                kite.load_session(ist("2026-10-29T09:00"), path)

    def test_no_session_file_asks_for_login(self):
        with self.assertRaisesRegex(kite.LoginNeeded, "login.py"):
            kite.load_session(ist("2026-10-29T09:00"), "/nonexistent/kite_session.json")


class Helpers(unittest.TestCase):
    def test_normalize_symbol_accepts_yahoo_and_kite_spellings(self):
        for raw in ("reliance", "RELIANCE.NS", "NSE:RELIANCE", " Reliance.BO "):
            self.assertEqual(kite.normalize_symbol(raw), "RELIANCE")

    def test_parse_instruments_keeps_equities_only(self):
        table = kite.parse_instruments(INSTRUMENTS)
        self.assertEqual(sorted(table), ["INFY", "RELIANCE"])
        self.assertEqual(table["RELIANCE"]["instrument_token"], 738561)
        self.assertEqual(table["RELIANCE"]["tick_size"], 0.1)

    def test_chunks_split_long_spans_without_gaps_or_overlap(self):
        start, end = ist("2026-01-01T09:15"), ist("2026-10-29T15:30")
        spans = list(kite.chunks(start, end, 100))
        self.assertEqual(spans[0][0], start)
        self.assertEqual(spans[-1][1], end)
        for (_, hi), (lo, _) in zip(spans, spans[1:]):
            self.assertEqual(lo - hi, datetime.timedelta(seconds=1))
        self.assertTrue(all(hi - lo < datetime.timedelta(days=100) for lo, hi in spans))

    def test_short_span_is_one_chunk(self):
        start, end = ist("2026-10-29T09:15"), ist("2026-10-29T15:30")
        self.assertEqual(list(kite.chunks(start, end, 60)), [(start, end)])

    def test_parse_candles_reads_kite_offsets_as_ist(self):
        data = {"candles": [["2026-10-29T09:15:00+0530", 1160, 1171.5, 1158.2, 1170, 523411]]}
        (t, o, h, l, c, v), = kite.parse_candles(data)
        self.assertEqual(t, ist("2026-10-29T09:15"))
        self.assertEqual((o, h, l, c, v), (1160.0, 1171.5, 1158.2, 1170.0, 523411))

    def test_dedupe_sorts_and_drops_repeats_at_chunk_edges(self):
        a = (ist("2026-10-29T09:20"), 1, 1, 1, 1, 1)
        b = (ist("2026-10-29T09:15"), 2, 2, 2, 2, 2)
        self.assertEqual(kite.dedupe([a, b, a]), [b, a])


class Http(Base):
    def test_get_sends_version_and_auth_headers_and_returns_data(self):
        self.net.answers.append(ok({"user_id": "AB1234"}))
        k = kite.Kite("key", "tok")
        self.assertEqual(k.profile(), {"user_id": "AB1234"})
        req = self.net.requests[0]
        self.assertEqual(req.get_header("X-kite-version"), "3")
        self.assertEqual(req.get_header("Authorization"), "token key:tok")

    def test_token_exception_becomes_login_needed(self):
        self.net.answers.append(http_error(403, "TokenException", "Incorrect api_key or token"))
        with self.assertRaisesRegex(kite.LoginNeeded, "Incorrect api_key"):
            kite.Kite("key", "tok").profile()

    def test_other_http_errors_name_the_endpoint(self):
        self.net.answers.append(http_error(500, "GeneralException", "Something went wrong"))
        with self.assertRaisesRegex(kite.KiteError, "/user/profile.*500.*Something"):
            kite.Kite("key", "tok").profile()

    def test_429_is_retried_with_a_pause_then_succeeds(self):
        waits = []
        kite._sleep = waits.append
        self.net.answers.extend([http_error(429, "NetworkException", "Too many requests"),
                                 ok({"user_id": "AB1234"})])
        self.assertEqual(kite.Kite("key", "tok").profile(), {"user_id": "AB1234"})
        self.assertEqual(len(self.net.requests), 2)
        self.assertIn(kite.BACKOFF_SECONDS, waits)

    def test_429_gives_up_after_the_retries(self):
        self.net.answers.extend([http_error(429, "NetworkException", "Too many requests")]
                                * (kite.RETRIES_ON_429 + 1))
        with self.assertRaisesRegex(kite.KiteError, "429"):
            kite.Kite("key", "tok").profile()
        self.assertEqual(len(self.net.requests), kite.RETRIES_ON_429 + 1)

    def test_network_down_is_a_clear_error(self):
        self.net.answers.append(urllib.error.URLError("no route"))
        with self.assertRaisesRegex(kite.KiteError, "cannot reach Kite"):
            kite.Kite("key", "tok").profile()

    def test_throttle_waits_between_quote_calls(self):
        waits = []
        kite._sleep = waits.append
        clock = iter([100.0, 100.0, 100.2, 100.2])
        saved = kite._clock
        kite._clock = lambda: next(clock)
        try:
            kite._throttle("quote")
            kite._throttle("quote")
        finally:
            kite._clock = saved
        self.assertEqual(len(waits), 1)
        self.assertAlmostEqual(waits[0], kite.MIN_GAP["quote"] - 0.2)


class MarketData(Base):
    def client(self, d):
        k = kite.Kite("key", "tok")
        self.net.answers.append(FakeResponse(INSTRUMENTS, "text/csv"))
        k.instruments("NSE", datetime.date(2026, 10, 29), d)
        return k

    def test_instruments_are_cached_on_disk_for_the_day(self):
        with tempfile.TemporaryDirectory() as d:
            self.client(d)
            again = kite.Kite("key", "tok").instruments("NSE", datetime.date(2026, 10, 29), d)
        self.assertEqual(len(self.net.requests), 1)
        self.assertIn("RELIANCE", again)

    def test_cas_symbols_are_stock_futures_underlyings_cached_daily(self):
        nfo = ("instrument_token,exchange_token,tradingsymbol,name,last_price,expiry,strike,"
               "tick_size,lot_size,instrument_type,segment,exchange\n"
               '1,1,RELIANCE26NOVFUT,"RELIANCE",0,2026-11-24,0,0.1,500,FUT,NFO-FUT,NFO\n'
               '2,2,NIFTY26NOVFUT,"NIFTY",0,2026-11-24,0,0.05,75,FUT,NFO-FUT,NFO\n'
               '3,3,INFY26NOV1500CE,"INFY",0,2026-11-24,1500,0.05,400,CE,NFO-OPT,NFO\n')
        self.net.answers.append(FakeResponse(nfo, "text/csv"))
        with tempfile.TemporaryDirectory() as d:
            first = kite.Kite("key", "tok").cas_symbols(datetime.date(2026, 10, 29), d)
            again = kite.Kite("key", "tok").cas_symbols(datetime.date(2026, 10, 29), d)
        self.assertEqual(first, {"RELIANCE"})
        self.assertEqual(again, {"RELIANCE"})
        self.assertEqual(len(self.net.requests), 1)

    def test_unknown_symbol_is_a_clear_error(self):
        with tempfile.TemporaryDirectory() as d:
            k = self.client(d)
            with self.assertRaisesRegex(kite.KiteError, "RELIANC is not listed on NSE"):
                k.instrument("RELIANC")

    def test_historical_uses_the_token_interval_and_ist_range(self):
        with tempfile.TemporaryDirectory() as d:
            k = self.client(d)
            self.net.answers.append(ok({"candles": [
                ["2026-10-29T09:15:00+0530", 1, 2, 0.5, 1.5, 10],
                ["2026-10-29T09:20:00+0530", 1.5, 2.5, 1, 2, 20]]}))
            bars = k.historical("RELIANCE.NS", "5minute", ist("2026-10-29T09:15"),
                                ist("2026-10-29T15:30"))
        url = self.net.requests[-1].full_url
        self.assertIn("/instruments/historical/738561/5minute?", url)
        self.assertIn("from=2026-10-29+09%3A15%3A00", url)
        self.assertEqual([b[4] for b in bars], [1.5, 2.0])

    def test_historical_rejects_unknown_intervals(self):
        with self.assertRaisesRegex(kite.KiteError, "unknown interval"):
            kite.Kite("key", "tok").historical("INFY", "2minute", ist("2026-10-29T09:15"),
                                               ist("2026-10-29T15:30"))

    def test_quotes_batch_500_and_key_by_symbol(self):
        syms = ["S%d" % i for i in range(501)]
        self.net.answers.extend([ok({"NSE:S0": {"last_price": 1}}), ok({"NSE:S500": {"last_price": 2}})])
        out = kite.Kite("key", "tok").quotes(syms)
        self.assertEqual(len(self.net.requests), 2)
        self.assertEqual(self.net.requests[0].full_url.count("i=NSE"), 500)
        self.assertEqual(out, {"S0": {"last_price": 1}, "S500": {"last_price": 2}})


class Login(Base):
    def test_request_token_from_redirect_url(self):
        url = "http://127.0.0.1:5000/callback?action=login&type=login&status=success&request_token=Rq1"
        self.assertEqual(login.request_token_from(url), "Rq1")

    def test_cancelled_login_gives_no_token(self):
        url = "http://127.0.0.1:5000/callback?status=cancelled&request_token=Rq1"
        self.assertEqual(login.request_token_from(url), "")

    def test_bare_token_is_accepted_and_junk_is_not(self):
        self.assertEqual(login.request_token_from(" Rq1 "), "Rq1")
        self.assertEqual(login.request_token_from("http://example.com/"), "")

    def test_remote_redirect_is_not_caught_locally(self):
        self.assertEqual(login.catch_redirect("https://example.com/callback"), "")

    def test_exchange_posts_the_checksum_and_saves_the_session(self):
        self.net.answers.append(ok({"access_token": "AT", "user_id": "AB1234", "user_name": "K"}))
        creds = {"api_key": "key", "api_secret": "sec", "redirect_url": ""}
        with tempfile.TemporaryDirectory() as d:
            cwd = os.getcwd()
            os.chdir(d)
            try:
                rec = login.exchange(creds, "Rq1", ist("2026-10-29T08:55"))
                saved = kite.load_session(ist("2026-10-29T09:00"))
            finally:
                os.chdir(cwd)
        req = self.net.requests[0]
        self.assertEqual(req.get_method(), "POST")
        self.assertIn(("checksum=" + kite.checksum("key", "Rq1", "sec")).encode(), req.data)
        self.assertEqual(rec["access_token"], "AT")
        self.assertEqual(saved["user_id"], "AB1234")


if __name__ == "__main__":
    unittest.main()
