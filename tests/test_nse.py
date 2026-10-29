import datetime
import unittest

from _load import load

nse = load("trader-kite", "nse")
D = datetime.date

# every weekday Kite had no RELIANCE daily candle for, Jan 1 - Oct 29 2026
SEEN_CLOSED_2026 = ["2026-01-15", "2026-01-26", "2026-03-03", "2026-03-26", "2026-03-31",
                    "2026-04-03", "2026-04-14", "2026-05-01", "2026-05-28", "2026-06-26",
                    "2026-09-14", "2026-10-02", "2026-10-20"]


def ist(text):
    return datetime.datetime.fromisoformat(text).replace(tzinfo=nse.IST)


class Calendar(unittest.TestCase):
    def test_holidays_match_the_days_kite_had_no_candle(self):
        listed = sorted(k for k in nse.NSE_HOLIDAYS if k <= "2026-10-29")
        self.assertEqual(listed, SEEN_CLOSED_2026)

    def test_holidays_are_all_weekdays(self):
        for k in nse.NSE_HOLIDAYS:
            self.assertLess(D.fromisoformat(k).weekday(), 5, k)

    def test_weekend_and_holiday_are_closed_with_a_reason(self):
        self.assertEqual(nse.trading_day(D(2026, 10, 31)), (False, "weekend"))
        self.assertEqual(nse.trading_day(D(2026, 11, 10)), (False, "NSE holiday: Diwali Balipratipada"))

    def test_muhurat_sunday_is_named_but_not_a_trading_day(self):
        ok, why = nse.trading_day(D(2026, 11, 8))
        self.assertFalse(ok)
        self.assertIn("Muhurat", why)

    def test_a_date_past_the_known_calendar_warns(self):
        ok, why = nse.trading_day(D(2027, 1, 4))
        self.assertTrue(ok)
        self.assertIn("2027", why)

    def test_previous_trading_days_skip_holidays(self):
        days = nse.previous_trading_days(D(2026, 10, 21), 2)
        self.assertEqual(days, [D(2026, 10, 16), D(2026, 10, 19)])


class Phases(unittest.TestCase):
    def test_phases_through_a_day_for_a_cas_stock(self):
        cases = [("08:59", "closed"), ("09:00", "pre-open"), ("09:14", "pre-open"),
                 ("09:15", "open"), ("15:14", "open"), ("15:15", "after-close")]
        for t, want in cases:
            self.assertEqual(nse.phase(ist("2026-10-29T%s" % t), cas=True), want, t)

    def test_non_cas_stock_trades_until_1530(self):
        self.assertEqual(nse.phase(ist("2026-10-29T15:20"), cas=False), "open")
        self.assertEqual(nse.phase(ist("2026-10-29T15:30"), cas=False), "after-close")

    def test_phase_converts_other_zones_to_ist(self):
        utc = datetime.datetime(2026, 10, 29, 4, 0, tzinfo=datetime.timezone.utc)  # 09:30 IST
        self.assertEqual(nse.phase(utc), "open")

    def test_holiday_is_closed_all_day(self):
        self.assertEqual(nse.phase(ist("2026-11-24T11:00")), "closed")

    def test_default_flat_by_is_before_zerodha_cas_square_off(self):
        self.assertLess(nse.DEFAULT_FLAT_BY, nse.MIS_SQUARE_OFF_CAS)

    def test_the_day_is_final_after_the_closing_auction(self):
        self.assertEqual(nse.CAS_END, datetime.time(15, 35))
        self.assertLess(nse.CLOSE, nse.CAS_END)


class NextOpen(unittest.TestCase):
    def test_before_the_open_is_today(self):
        self.assertEqual(nse.next_open(ist("2026-10-29T08:00")), ist("2026-10-29T09:15"))

    def test_friday_evening_is_monday(self):
        self.assertEqual(nse.next_open(ist("2026-10-30T16:00")), ist("2026-11-02T09:15"))

    def test_skips_diwali(self):
        self.assertEqual(nse.next_open(ist("2026-11-09T16:00")), ist("2026-11-11T09:15"))


class ParseTime(unittest.TestCase):
    def test_24h_and_12h_forms(self):
        self.assertEqual(nse.parse_hhmm("15:05"), datetime.time(15, 5))
        self.assertEqual(nse.parse_hhmm("3:05 pm"), datetime.time(15, 5))
        self.assertEqual(nse.parse_hhmm("12:00 AM"), datetime.time(0, 0))

    def test_rejects_junk(self):
        for bad in ("25:00", "9:75", "noon"):
            with self.assertRaises(ValueError):
                nse.parse_hhmm(bad)


if __name__ == "__main__":
    unittest.main()
