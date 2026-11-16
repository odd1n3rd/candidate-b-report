from __future__ import annotations

import argparse
import csv
import re
from collections import defaultdict
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import date
from pathlib import Path


@dataclass(frozen=True)
class Contract:
    contract_id: str
    contractor: str
    amount: int
    signed_at: date


@dataclass(frozen=True)
class Work:
    work_id: str
    contract_id: str
    title: str
    price: int
    completed_at: date | None
    cancelled_at: date | None

    def status_on(self, as_of: date) -> str:
        if self.cancelled_at is not None and self.cancelled_at <= as_of:
            return "cancelled"
        if self.completed_at is not None and self.completed_at <= as_of:
            return "done"
        return "planned"


@dataclass(frozen=True)
class Payment:
    payment_id: str
    work_id: str
    amount: int
    paid_at: date | None
    refunded_at: date | None


@dataclass(frozen=True)
class WorkLine:
    work: Work
    status: str
    direct: int
    advance: int

    @property
    def paid(self) -> int:
        return self.direct + self.advance

    @property
    def remaining(self) -> int:
        if self.status == "cancelled":
            return 0
        return max(0, self.work.price - self.paid)


@dataclass(frozen=True)
class ReconciliationLine:
    work_id: str
    reason: str
    amount: int
    payment_ids: tuple[str, ...]


@dataclass(frozen=True)
class ContractReport:
    contract: Contract
    works: list[WorkLine]
    gross_paid: int
    refunds: int
    free_advance: int
    reconciliation: list[ReconciliationLine]

    @property
    def paid(self) -> int:
        return self.gross_paid - self.refunds

    @property
    def active_price(self) -> int:
        return sum(line.work.price for line in self.works if line.status != "cancelled")

    @property
    def work_remainder(self) -> int:
        return sum(line.remaining for line in self.works)

    @property
    def to_transfer(self) -> int:
        return max(0, self.work_remainder - self.free_advance)


def read_rows(path: Path, columns: str) -> Iterator[tuple[str, dict[str, str]]]:
    required = columns.split()
    id_field = required[0]
    seen_ids = set()
    with path.open(encoding="utf-8-sig", newline="") as file:
        reader = csv.DictReader(file, strict=True)
        try:
            header = reader.fieldnames or []
            missing = set(required) - set(header)
            if missing:
                raise ValueError(f"{path.name}: отсутствуют колонки {', '.join(sorted(missing))}")
            if len(header) != len(set(header)):
                raise ValueError(f"{path.name}: повторяющиеся названия колонок")

            for row in reader:
                where = f"{path.name}:{reader.line_num}"
                if None in row or None in row.values():
                    raise ValueError(f"{where}: число значений отличается от числа колонок")
                if not row[id_field].strip() or row[id_field] in seen_ids:
                    raise ValueError(f"{where}: пустой или повторяющийся {id_field} {row[id_field]!r}")
                seen_ids.add(row[id_field])
                yield where, row
        except csv.Error as error:
            raise ValueError(f"{path.name}:{reader.line_num}: {error}") from error


def iso_date(value: str) -> date:
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        raise ValueError(value)
    return date.fromisoformat(value)


def parse_date(row: dict[str, str], field: str, where: str) -> date:
    try:
        return iso_date(row[field])
    except ValueError as error:
        raise ValueError(f"{where}: некорректная дата в {field} {row[field]!r}") from error


def parse_money(row: dict[str, str], field: str, where: str) -> int:
    value = row[field]
    if not re.fullmatch(r"[0-9]+(?:\.[0-9]{1,2})?", value):
        raise ValueError(f"{where}: некорректная сумма в {field} {value!r}")
    rubles, _, kopecks = value.partition(".")
    return 100 * int(rubles) + int(kopecks.ljust(2, "0"))


def build_report(data_dir: Path, as_of: date) -> list[ContractReport]:
    contracts = {}
    for where, row in read_rows(data_dir / "contracts.csv", "contract_id contractor amount signed_at"):
        contract_id = row["contract_id"]
        contracts[contract_id] = Contract(
            contract_id, row["contractor"],
            parse_money(row, "amount", where), parse_date(row, "signed_at", where),
        )

    works_by_id = {}
    works_by_contract = defaultdict(list)
    work_columns = "work_id contract_id title price status completed_at cancelled_at"
    for where, row in read_rows(data_dir / "works.csv", work_columns):
        work_id, contract_id, status = row["work_id"], row["contract_id"], row["status"]
        if contract_id not in contracts:
            raise ValueError(f"{where}: неизвестный договор {contract_id!r}")
        if status not in {"planned", "done", "cancelled"}:
            raise ValueError(f"{where}: неизвестный статус работы {status!r}")
        completed = parse_date(row, "completed_at", where) if status == "done" else None
        cancelled = parse_date(row, "cancelled_at", where) if status == "cancelled" else None
        work = Work(work_id, contract_id, row["title"], parse_money(row, "price", where), completed, cancelled)
        works_by_id[work_id] = work
        works_by_contract[contract_id].append(work)

    payments_by_contract = defaultdict(list)
    payment_columns = "payment_id contract_id work_id purpose amount status created_at paid_at refunded_at"
    for where, row in read_rows(data_dir / "payments.csv", payment_columns):
        payment_id, contract_id, work_id = row["payment_id"], row["contract_id"], row["work_id"]
        status = row["status"]
        if contract_id not in contracts:
            raise ValueError(f"{where}: неизвестный договор {contract_id!r}")
        if work_id and (work_id not in works_by_id or works_by_id[work_id].contract_id != contract_id):
            raise ValueError(f"{where}: работа {work_id!r} отсутствует в договоре {contract_id!r}")
        if status not in {"queued", "paid", "refunded"}:
            raise ValueError(f"{where}: неизвестный статус платежа {status!r}")
        paid = parse_date(row, "paid_at", where) if status in {"paid", "refunded"} else None
        refunded = parse_date(row, "refunded_at", where) if status == "refunded" else None
        if refunded is not None and refunded < paid:
            raise ValueError(f"{where}: возврат раньше выплаты")
        payment = Payment(payment_id, work_id, parse_money(row, "amount", where), paid, refunded)
        payments_by_contract[contract_id].append(payment)

    return [
        calculate_contract(contract, works_by_contract[contract_id], payments_by_contract[contract_id], as_of)
        for contract_id, contract in sorted(contracts.items())
        if contract.signed_at <= as_of
    ]


def calculate_contract(
    contract: Contract, works: list[Work], payments: list[Payment], as_of: date
) -> ContractReport:
    gross_paid = refunds = free_advance = 0
    direct_by_work = defaultdict(int)
    direct_ids = defaultdict(list)
    for payment in payments:
        if payment.paid_at is None or payment.paid_at > as_of:
            continue
        gross_paid += payment.amount
        if payment.refunded_at is not None and payment.refunded_at <= as_of:
            refunds += payment.amount
        elif payment.work_id:
            direct_by_work[payment.work_id] += payment.amount
            if payment.amount:
                direct_ids[payment.work_id].append(payment.payment_id)
        else:
            free_advance += payment.amount

    lines = []
    reconciliation = []
    for work in sorted(works, key=lambda item: (item.completed_at or date.max, item.work_id)):
        status = work.status_on(as_of)
        direct = direct_by_work[work.work_id]
        advance = 0
        if status == "done":
            advance = min(free_advance, max(0, work.price - direct))
            free_advance -= advance
        lines.append(WorkLine(work, status, direct, advance))

        if status == "cancelled":
            reason, amount = "cancelled", direct
        else:
            reason, amount = "overpaid", max(0, direct - work.price)
        if amount:
            payment_ids = tuple(sorted(direct_ids[work.work_id]))
            reconciliation.append(ReconciliationLine(work.work_id, reason, amount, payment_ids))

    lines.sort(key=lambda line: line.work.work_id)
    reconciliation.sort(key=lambda line: line.work_id)
    return ContractReport(contract, lines, gross_paid, refunds, free_advance, reconciliation)


def money(amount: int) -> str:
    rubles, kopecks = divmod(amount, 100)
    return f"{rubles:,}".replace(",", " ") + f",{kopecks:02d}"


def print_table(header: tuple[str, ...], rows: Iterable[tuple[str, ...]], numeric_from: int) -> None:
    table = [header, *rows]
    widths = [max(map(len, column)) for column in zip(*table)]
    for row in table:
        cells = [
            cell.rjust(width) if index >= numeric_from else cell.ljust(width)
            for index, (cell, width) in enumerate(zip(row, widths))
        ]
        print("  ".join(cells).rstrip())


def print_report(as_of: date, reports: list[ContractReport]) -> None:
    print(f"Отчёт на {as_of:%d.%m.%Y}. Суммы в рублях.")
    if not reports:
        print("На эту дату подписанных договоров нет.")
        return

    status_names = {"done": "Принята", "planned": "План", "cancelled": "Отменена"}
    reasons = {"cancelled": "Оплата отменённой работы", "overpaid": "Прямая переплата"}
    for result in reports:
        contract = result.contract
        print(f"\nДоговор {contract.contract_id} | {contract.contractor}")
        if result.works:
            print_table(
                ("Работа", "Название", "Статус", "Цена", "Прямая оплата", "Зачёт аванса", "Выплачено", "Остаток"),
                (
                    (
                        line.work.work_id, line.work.title, status_names[line.status], money(line.work.price),
                        money(line.direct), money(line.advance), money(line.paid), money(line.remaining),
                    )
                    for line in result.works
                ),
                numeric_from=3,
            )
        else:
            print("Работ в выгрузке нет.")

        print("\nИтоги по договору")
        totals = (
            ("Номинал договора", contract.amount),
            ("Цена действующих работ", result.active_price),
            ("Выплачено до возвратов", result.gross_paid),
            ("Возвращено", result.refunds),
            ("Чистые фактические выплаты", result.paid),
            ("Свободный аванс", result.free_advance),
            ("Остаток по работам", result.work_remainder),
            ("К перечислению", result.to_transfer),
        )
        for label, amount in totals:
            print(f"{label:<28} {money(amount):>14}")

        if result.reconciliation:
            print("\nСуммы на сверку")
            print_table(
                ("Работа", "Причина", "Платежи", "Сумма"),
                (
                    (line.work_id, reasons[line.reason], ", ".join(line.payment_ids), money(line.amount))
                    for line in result.reconciliation
                ),
                numeric_from=3,
            )


def report_date(value: str) -> date:
    if re.fullmatch(r"[0-9]{2}\.[0-9]{2}\.[0-9]{4}", value):
        day, month, year = value.split(".")
        value = f"{year}-{month}-{day}"
    try:
        return iso_date(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("укажи дату в формате ДД.ММ.ГГГГ или ГГГГ-ММ-ДД") from error


def main() -> int:
    parser = argparse.ArgumentParser(description="Отчёт по оплате работ на указанную дату", allow_abbrev=False)
    parser.add_argument("date", metavar="ДАТА", type=report_date)
    parser.add_argument(
        "--data-dir", metavar="ПАПКА", type=Path, default=Path(__file__).resolve().parent / "data",
        help="папка с входными CSV (по умолчанию data рядом со скриптом)",
    )
    args = parser.parse_args()
    try:
        reports = build_report(args.data_dir, args.date)
    except (OSError, ValueError) as error:
        parser.exit(2, f"Ошибка входных данных: {error}\n")
    print_report(args.date, reports)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
