import subprocess
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

from report import build_report


DATA_DIR = Path(__file__).resolve().parent / "data"


def reports_on(as_of, data_dir=DATA_DIR):
    return {
        result.contract.contract_id: result
        for result in build_report(data_dir, date.fromisoformat(as_of))
    }


def work_line(result, work_id):
    return next(line for line in result.works if line.work.work_id == work_id)


class ProvidedDataTests(unittest.TestCase):
    def test_requested_snapshot(self):
        reports = reports_on("2026-11-10")
        for contract_id, expected in {
            "D-1": (31_500_000, 1_500_000, 20_000_000, 18_500_000),
            "D-2": (7_500_000, 2_500_000, 4_000_000, 1_500_000),
        }.items():
            with self.subTest(contract=contract_id):
                result = reports[contract_id]
                self.assertEqual(
                    (result.paid, result.free_advance, result.work_remainder, result.to_transfer),
                    expected,
                )

    def test_refund_reduces_cash_on_refund_date(self):
        before = reports_on("2026-09-09")["D-1"]
        after = reports_on("2026-09-10")["D-1"]
        self.assertEqual((before.gross_paid, before.refunds, before.free_advance), (35_000_000, 0, 35_000_000))
        self.assertEqual((after.gross_paid, after.refunds, after.free_advance), (35_000_000, 5_000_000, 30_000_000))

    def test_later_payment_counts_from_its_paid_date(self):
        before = reports_on("2026-11-11")["D-2"]
        after = reports_on("2026-11-12")["D-2"]
        self.assertEqual((before.paid, before.to_transfer), (7_500_000, 1_500_000))
        self.assertEqual((after.paid, after.to_transfer, after.free_advance), (9_500_000, 0, 500_000))
        self.assertEqual(work_line(after, "W-23").advance, 4_000_000)

    def test_future_acceptance_does_not_receive_advance(self):
        before = work_line(reports_on("2026-11-10")["D-2"], "W-23")
        after = work_line(reports_on("2026-11-11")["D-2"], "W-23")
        self.assertEqual((before.status, before.advance, before.remaining), ("planned", 0, 4_000_000))
        self.assertEqual((after.status, after.advance, after.remaining), ("done", 2_500_000, 1_500_000))

    def test_cancellation_removes_price_from_obligations_on_its_date(self):
        before = reports_on("2026-10-11")["D-1"]
        after = reports_on("2026-10-12")["D-1"]
        self.assertEqual(before.active_price, 60_000_000)
        self.assertEqual(after.active_price, 50_000_000)
        self.assertEqual(work_line(before, "W-13").remaining, 10_000_000)
        cancelled = work_line(after, "W-13")
        self.assertEqual((cancelled.status, cancelled.work.price, cancelled.remaining), ("cancelled", 10_000_000, 0))

    def test_direct_payment_stays_on_work_and_counts_before_acceptance(self):
        before = work_line(reports_on("2026-10-24")["D-1"], "W-14")
        paid = work_line(reports_on("2026-10-25")["D-1"], "W-14")
        accepted = work_line(reports_on("2026-10-28")["D-1"], "W-14")
        self.assertEqual(before.direct, 0)
        self.assertEqual((paid.direct, paid.advance, paid.remaining), (1_500_000, 0, 8_500_000))
        self.assertEqual((accepted.direct, accepted.advance, accepted.remaining), (1_500_000, 8_500_000, 0))


class ReconciliationTests(unittest.TestCase):
    def setUp(self):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        self.data_dir = Path(temp_dir.name)
        (self.data_dir / "contracts.csv").write_text(
            "contract_id,contractor,amount,signed_at\nC,Test,53.00,2026-09-01\n",
            encoding="utf-8",
        )
        (self.data_dir / "works.csv").write_text(
            "work_id,contract_id,title,price,status,completed_at,cancelled_at\n"
            "E,C,Same date,1.00,done,2026-09-04,\n"
            "A,C,Later,20.00,done,2026-09-04,\n"
            "B,C,Overpaid,10.00,done,2026-09-01,\n"
            "C,C,Cancelled,15.00,cancelled,,2026-09-05\n"
            "D,C,Planned,5.00,planned,,\n"
            "F,C,Earlier,2.00,done,2026-09-03,\n",
            encoding="utf-8",
        )
        self.payments_path = self.data_dir / "payments.csv"
        self.payments_path.write_text(
            "payment_id,contract_id,work_id,purpose,amount,status,created_at,paid_at,refunded_at\n"
            "P1,C,A,Direct,2.02,paid,2026-09-02,2026-09-02,\n"
            "P2,C,B,Direct,12.50,paid,2026-09-02,2026-09-02,\n"
            "P3,C,C,Direct,4.00,paid,2026-09-02,2026-09-02,\n"
            "P4,C,,Advance,3.01,paid,2026-09-02,2026-09-02,\n",
            encoding="utf-8",
        )

    def test_reconciliation_money_does_not_cover_other_works(self):
        result = reports_on("2026-09-05", self.data_dir)["C"]
        self.assertEqual((result.paid, result.free_advance), (2153, 0))
        self.assertEqual((result.work_remainder, result.to_transfer), (2297, 2297))
        self.assertEqual((work_line(result, "B").direct, work_line(result, "B").remaining), (1250, 0))
        self.assertEqual((work_line(result, "C").direct, work_line(result, "C").remaining), (400, 0))
        self.assertEqual(
            [(line.work_id, line.reason, line.amount, line.payment_ids) for line in result.reconciliation],
            [("B", "overpaid", 250, ("P2",)), ("C", "cancelled", 400, ("P3",))],
        )

    def test_cli_shows_reconciliation_with_payment_ids(self):
        result = subprocess.run(
            [sys.executable, "-B", str(DATA_DIR.parent / "report.py"), "05.09.2026",
             "--data-dir", str(self.data_dir)],
            cwd=self.data_dir, capture_output=True, encoding="utf-8", check=True,
        )
        self.assertRegex(result.stdout, r"(?m)^B +Прямая переплата +P2 +2,50$")
        self.assertRegex(result.stdout, r"(?m)^C +Оплата отменённой работы +P3 +4,00$")
        self.assertRegex(result.stdout, r"(?m)^К перечислению +22,97$")

    def test_advance_follows_acceptance_date_then_work_id(self):
        result = reports_on("2026-09-05", self.data_dir)["C"]
        advances = [work_line(result, work_id).advance for work_id in ("F", "A", "E")]
        self.assertEqual(advances, [200, 101, 0])

    def test_refunded_direct_payment_stops_covering_its_work(self):
        with self.payments_path.open("a", encoding="utf-8") as payments:
            payments.write("P5,C,D,Direct,5.00,refunded,2026-09-02,2026-09-03,2026-09-05\n")
        before = work_line(reports_on("2026-09-04", self.data_dir)["C"], "D")
        after = work_line(reports_on("2026-09-05", self.data_dir)["C"], "D")
        self.assertEqual((before.direct, before.remaining), (500, 0))
        self.assertEqual((after.direct, after.remaining), (0, 500))


if __name__ == "__main__":
    unittest.main()
