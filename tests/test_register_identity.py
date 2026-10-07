"""The order's own number and date come from the GOIR register, never from
its Read list.

    python3 -m unittest discover -s tests

Row 445791 was stored as G.O.Rt.No.165 of 22-04-2021; it is G.O.Rt.No.500 of
24-06-2022, and 165 / 22-04-2021 is the first document its Read list cites.
The fixtures below are that order's shape and the layouts the daily sync met
between 22 Jun and 5 Oct 2026 (53 of 7,067 rows took a Read-list value).
"""
from __future__ import annotations

import datetime as dt
import sys
import tempfile
import unittest
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import pipeline as P  # noqa: E402

# Row 445791's header, as OCR reads it (HMF01-RT-500-24_06_2022). Its date is
# printed ABOVE the number line, and the parser looks for "Dated" only from the
# number line down, so it takes the date of item 1 of the Read list.
READ_LIST_ORDER = """GOVERNMENT OF ANDHRA PRADESH
ABSTRACT
HM&FW Department - Permission for establishment of Vijaya Medical Centre
Institute of Paramedical Sciences at Visakhapatnam - Orders - Issued.
HEALTH, MEDICAL & FAMILY WELFARE (G.2) DEPARTMENT
Dated.24.06.2022.
G.O.Rt.No.500.
Read the following:-
1. G.O.Rt.No. 165, HM&FW C.1) Department, Dated.22.04.2021.
2. From the Chairman, High Power Committee, letter Rc.No.229/HPC/2018, Dated:24.03.2022
ORDER:-
In the reference 2nd read above, the Chairman, High Power Committee has submitted
the inspection report for establishment of the Institute.
"""

# FIN01-RT-2920-30_09_2026, loaded by the daily sync on 4 Oct 2026 as
# G.O.Rt.No.101 of 2026-09-30. The page prints each value above its label, so
# "G.O.Rt.No:" has no number after it and the parser takes item 1's: one of 26
# Finance orders stored as No.101 (the Read list's G.O.Ms.No.101 of 12-08-2015).
NUMBER_IN_READ_LIST = """Page 1 Of (3)
           GOVERNMENT OF ANDHRA PRADESH
                              ABSTRACT
2920
G.O.Rt.No:
30-09-2026
Dated:
FINANCE ( FMU-I&I,ENERGY,I&C ) DEPARTMENT
Read the following:-
Budget (2026-27)-GRANT NO. XXXVI-INDUSTRIES & COMMERCE, INFRASTRUCTURE AND
INVESTMENT-REAPPROPRIATION/RESUMPTION/RE-DISTRIBUTION OF FUNDS-ORDERS-
ISSUED
        1 . GO Ms.No.101 , Finance (Budget-I) Department , 12-08-2015
        2 . GO Ms.No.1 , Finance (Budget-I) Department , 02-01-2026
        3 . GO Ms.No.18 , Finance (Budget-I) Department , 30-03-2026
ORDER:
The following Reappropriation / Resumption of funds are ordered for the purpose of
administrative expenditure of the Department, as detailed in the annexure to this order.
"""

# MAU01-RT-877-08_07_2026: "Date:" rather than "Dated", and the parser took item
# 1's date, 13 days earlier: inside the old 31-day guard, so it was stored.
WITHIN_A_MONTH = """GOVERNMENT OF ANDHRA PRADESH
ABSTRACT
Municipal Administration & Urban Development Department - Policy on Reuse of Treated Used Water, 2026 - Orders - Issued.
MUNICIPAL ADMINISTRATION & URBAN DEVELOPMENT (K) DEPARTMENT
G.O.RT.No.877
  Date:08.07.2026.
  Read the following:-
1. G.O.Ms.No.129, MA&UD (K) Department, dated:25.06.2026.
2. From the Managing Director, APUFIDC Ltd., Lr.No.APUF-11025, Dt:03.07.2026.
ORDER:-
In the reference 1st read above, Government have notified the Policy on Reuse of Treated Used Water.
"""

# HMF01-MS-91-10_07_2026: a text layer out of reading order prints the items
# above the "Read the following" heading, and the number line below them.
SCRAMBLED = """GOVERNMENT OF ANDHRA PRADESH
ABSTRACT
Establishment - Regularization of Services of the Contract Employees - Orders - Issued.
HEALTH, MEDICAL & FAMILY WELFARE (G2) DEPARTMENT
     1. Andhra Pradesh Regularisation of Services of Contract Employees Act-2023 (Act 30 of 2023).
     2. G.O.Ms.No.114, Finance (HR. I - Plg. & Policy) Department, dated.21.10.2023.
ORDER:
G.O.Ms.No. 91
Dated:10/07/2026
Read the following:
     The Government of Andhra Pradesh have enacted the Act.
"""

AGREES = """GOVERNMENT OF ANDHRA PRADESH
ABSTRACT
BC Welfare Department - Promotion and Postings - Orders - Issued.
BACKWARD CLASSES WELFARE (C) DEPARTMENT
G. O. RT. No.129
Dated: 30.09.2026
Read the following:
1. G. O. Rt. No. 29, B. C. Welfare (C) Department, dt: 12.02.2024.
ORDER:
Government hereby order accordingly.
"""


def parse(text: str) -> tuple[dict, str]:
    """The parser's own reading of a header, and the text the sync keeps."""
    rec = P.gp.parse_text(Path("fixture.pdf"), text, 1)
    return rec.get("go_meta") or {}, P.head_text(text)


class ReadListOrder(unittest.TestCase):
    def test_parser_reads_the_cited_values(self):
        # The defect itself, so this file fails loudly if the parser changes.
        gm, _ = parse(READ_LIST_ORDER)
        self.assertEqual(gm["go_date"]["iso"], "2021-04-22")
        gm, _ = parse(NUMBER_IN_READ_LIST)
        self.assertEqual(gm["go_number"], 101)

    def test_register_date_is_stored(self):
        gm, head = parse(READ_LIST_ORDER)
        got = P.register_identity(500, dt.date(2022, 6, 24), gm, head)
        self.assertEqual((got["go_number"], got["go_date"]), (500, "2022-06-24"))
        # The header printed 500 too, so the number's source is the document.
        self.assertEqual(got["go_number_source"], "document_header")
        self.assertEqual(got["go_date_source"], "date_uploaded_goir_portal")
        self.assertEqual(got["date_at"], "read_list")
        self.assertIn("document_date_from_read_list", got["warnings"])
        # 428 days out: the site's "more than a month" sentence is true.
        self.assertIn("document_date_disagrees_with_register", got["warnings"])
        self.assertIn("Read list", got["go_date_remark"])

    def test_register_number_is_stored(self):
        gm, head = parse(NUMBER_IN_READ_LIST)
        got = P.register_identity(2920, dt.date(2026, 9, 30), gm, head)
        self.assertEqual((got["go_number"], got["go_date"]), (2920, "2026-09-30"))
        self.assertEqual(got["go_number_source"], "goir_register")
        self.assertEqual(got["number_at"], "read_list")
        self.assertIn("document_number_from_read_list", got["warnings"])

    def test_read_list_values_never_fill_a_gap(self):
        gm, head = parse(READ_LIST_ORDER)
        got = P.register_identity(None, None, gm, head)
        self.assertEqual(got["go_number"], 500)      # printed before the Read list
        self.assertIsNone(got["go_date"])            # printed only inside it
        gm, head = parse(NUMBER_IN_READ_LIST)
        self.assertIsNone(P.register_identity(None, None, gm, head)["go_number"])

    def test_read_list_date_within_a_month(self):
        gm, head = parse(WITHIN_A_MONTH)
        self.assertEqual(gm["go_date"]["iso"], "2026-06-25")
        got = P.register_identity(877, dt.date(2026, 7, 8), gm, head)
        self.assertEqual(got["go_date"], "2026-07-08")
        self.assertEqual(got["go_number_source"], "document_header")
        self.assertIn("document_date_from_read_list", got["warnings"])
        self.assertNotIn("document_date_disagrees_with_register", got["warnings"])

    def test_items_printed_above_their_heading(self):
        head = P.head_text(SCRAMBLED)
        self.assertEqual(P.read_list_start(head), head.index("     1. Andhra"))
        self.assertEqual(P.where_printed(114, head, P._numbers_in), "read_list")
        self.assertEqual(P.where_printed("2023-10-21", head, P._dates_in), "read_list")

    def test_header_agreeing_with_register(self):
        gm, head = parse(AGREES)
        got = P.register_identity(129, dt.date(2026, 9, 30), gm, head)
        self.assertEqual((got["go_number"], got["go_date"]), (129, "2026-09-30"))
        self.assertEqual((got["go_number_source"], got["go_date_source"]),
                         ("document_header", "document_header"))
        self.assertEqual(got["warnings"], [])
        self.assertIsNone(got["go_date_remark"])

    def test_header_fills_only_what_the_register_lacks(self):
        gm, head = parse(AGREES)
        got = P.register_identity(None, None, gm, head)
        self.assertEqual((got["go_number"], got["go_date"]), (129, "2026-09-30"))
        self.assertEqual((got["number_at"], got["date_at"]), ("header", "header"))

    def test_register_wins_a_header_date_that_differs(self):
        # BCW01-RT-129-30_09_2026 prints "Dated: 29.09.2026" beside its own
        # number. The register's date is stored; the header's is kept in the remark.
        gm, head = parse(AGREES.replace("30.09.2026", "29.09.2026"))
        got = P.register_identity(129, dt.date(2026, 9, 30), gm, head)
        self.assertEqual(got["go_date"], "2026-09-30")
        self.assertEqual(got["go_date_source"], "date_uploaded_goir_portal")
        self.assertEqual(got["warnings"], ["document_date_differs_from_register"])
        self.assertIn("2026-09-29", got["go_date_remark"])


class BuildRow(unittest.TestCase):
    """The whole path the sync takes: a PDF on disk, parse_one, build_row."""

    def test_read_list_number_end_to_end(self):
        name = "FIN01 - FINANCE-RT-2920-30_09_2026.pdf"
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / name
            doc = P.gp.fitz.open()
            doc.new_page().insert_text((30, 40), NUMBER_IN_READ_LIST, fontsize=7)
            doc.save(path)
            doc.close()
            res = P.parse_one(str(path))
        self.assertEqual(res["rec"]["go_meta"]["go_number"], 101)
        listing = {"go_date_raw": "30/09/2026", "go_number_raw": "FIN01-2920", "go_type": "RT",
                   "organization": "FIN01 - FINANCE", "filename": name, "gid_english": "1",
                   "abstract": "", "goir_category": "Others"}
        counts: Counter = Counter()
        row, _, _ = P.build_row(listing, res, {"FINANCE": 1}, {"FIN01": "FINANCE"}, counts, [])
        self.assertEqual((row["go_number"], row["go_date"], row["go_year"]), (2920, "2026-09-30", 2026))
        self.assertEqual(row["go_number_goir_register"], 2920)
        self.assertEqual(row["go_number_source"], "goir_register")
        self.assertIsNone(row["go_number_remark"])
        # The parser read "Ms" off the cited G.O.; this order is an RT.
        self.assertIsNone(row["go_type_document"])
        self.assertNotIn("filename_body_mismatch:go_type(body=MS,filename=RT)", row["warnings"])
        self.assertIn("document_number_from_read_list", row["warnings"])
        self.assertEqual(counts["document_number_from_read_list"], 1)


if __name__ == "__main__":
    unittest.main()
