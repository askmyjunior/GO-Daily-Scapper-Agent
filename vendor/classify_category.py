#!/usr/bin/env python3
"""Classify every GO into the approved AskMyJunior taxonomy -> out/category/.

One row per corpus record: go_category, go_sub_category, secondary_tags[],
with verbatim evidence for every decision. A sidecar in the standing pattern:

  - The record is NEVER modified. The abstract is read, not rewritten.
  - Every record is classified INDEPENDENTLY: classify() is a pure function
    of one record's own text. No neighbour, no carry-forward, no department
    default. Record-independence holds by construction, not by discipline.
  - The primary source is the record's own `abstract`. Where the abstract is
    absent, the head of the record's own `order_text` is read instead and the
    row says so (`basis`). Where neither exists the row is flagged
    `no_text_to_classify` -- flagged, never guessed.
  - Deterministic phrase rules, no LLM. Rows the rules cannot settle are
    flagged `no_rule_matched` / `ambiguous` for the review stage; a flag is a
    labelled state, not a failure.

HOW THE GRAMMAR IS READ. A GO abstract is a chain of dash-separated segments
ending in boilerplate ("Orders - Issued"). The OPERATIVE ACTION (sanction,
amendment, appointment, constitution...) sits in the trailing segments; the
leading segments name the subject (department, scheme, land, person). So:
the primary category comes from the action match closest to the end of the
abstract; subject matches elsewhere become secondary tags.

LEGAL ACTIONS ARE OBJECT-SENSITIVE (user instruction: distinguish, never lump
as "Amendment"): an amendment's object decides GO-vs-Act/Rules sub-category;
"Modification to the Master Plan" is not an Amendment/Modification GO at all;
"Transfer of ... road" is not Transfers & Postings; "Delegation of powers" is
not travel; "Suspension" of a person is Disciplinary, of a GO is the
Amendment family.

Usage:
  python3 classify_category.py --sample 400      # stratified sample, printed
  python3 classify_category.py --apply           # write out/category/*.jsonl
"""
from __future__ import annotations

import argparse
import json
import random
import re
from collections import Counter
from pathlib import Path

OUT_DIR = Path("out/category")

# ---------------------------------------------------------------------------
# The approved taxonomy, verbatim. Categories and sub-categories come ONLY
# from this table; proposing additions is report_category_proposals' job,
# never this file's.
# ---------------------------------------------------------------------------
TAXONOMY = {
    "Budget Release": [
        "Allocation", "Release", "Sanction", "Re-appropriation",
        "Provision of Government Funds / Budget", "Other Budget Matters"],
    "Service Matters": [
        "Appointment", "Recruitment", "Promotion", "Seniority",
        "Pay & Allowances", "Leave", "Pension", "Retirement",
        "Disciplinary Matters", "Service Conditions", "Deputation",
        "Posts & Establishment",     # user-approved addition 2026-09-19
        "Other Service Matters"],
    "Tours & Travel": [
        "Official Tour", "Travel Permission", "Foreign Visit",
        "Domestic Visit", "Delegation", "Travel Allowance",
        "Other Travel Matters"],
    "Transfers & Postings": [
        "Transfer", "Posting", "Deputation", "Relieving", "Joining",
        "Reallocation of Personnel", "Other Transfer / Posting Matters"],
    "General Provident Fund (GPF)": [
        "GPF Subscription", "GPF Advance", "GPF Withdrawal",
        "GPF Final Settlement", "GPF Nomination", "Other GPF Matters"],
    "eOffice / Administrative Processing": [
        "eOffice Processing", "Administrative Approval",
        "Administrative Instructions", "Procedural Matters",
        "Other Administrative Processing"],
    "Implementation / Effective Date": [
        "Implementation", "Effective Date", "Commencement",
        "Operationalisation", "Enforcement"],
    "Amendment / Modification": [
        "Amendment of GO", "Suspension of GO",
        "Revocation / Cancellation of GO", "Withdrawal of GO",
        "Modification of GO", "Extension / Continuation of GO",
        "Supersession of GO", "Amendment of Act / Rules",
        "Revocation / Repeal of Act / Rules",
        "Implementation of Act / Rules", "Other Legislative / GO Changes"],
    "Enactment / Framing of Act & Rules": [
        "Enactment of Act", "Framing of Rules", "Making of Regulations",
        "Notification of Act / Rules",
        "Other New Legislative / Regulatory Frameworks"],
    "Introduction / Formation": [
        "Formation of Authority", "Constitution of Committee",
        "Establishment of Institution", "Creation of Body",
        "Introduction of Policy", "Other New Administrative Frameworks"],
    "Scheme / Programme": [
        "Welfare Scheme", "Government Programme", "Mission", "Subsidy",
        "Benefit", "Financial Assistance", "Scheme Guidelines",
        "Scheme Implementation", "Other Scheme / Programme Matters"],
    # --- the four additions the user approved on 2026-09-19, proposed from
    # --- the measured Others bucket (report_category_proposals) -----------
    "Land & Property Matters": [
        "Land Acquisition", "Land Allotment / Alienation / Assignment",
        "Land Transfer / Resumption", "Urban Planning & Land Use",
        "Government Accommodation / Quarters",
        "Other Land & Property Matters"],
    "Licences, Permissions & Exemptions": [
        "Permission / NOC", "Licence", "Exemption / Relaxation",
        "Other Licence / Permission Matters"],
    "Works & Projects": [
        "Administrative Approval of Works", "Tenders / Entrustment",
        "Project Execution", "Other Works / Project Matters"],
    "Mines & Minerals": [
        "Mining Lease", "Quarry Lease", "Mineral Concessions",
        "Other Mines & Minerals Matters"],
    "Others": [None],
}

# ---------------------------------------------------------------------------
# Text mechanics
# ---------------------------------------------------------------------------
BOILER = re.compile(
    r"^(?:orders?|issued|notification|reg\.?|amendment orders issued"
    r"|[\W_]*)$", re.I)
# split on en/em dashes, or a hyphen with whitespace on at least one side
SEG_SPLIT = re.compile(r"\s*[–—]\s*|\s+-\s*|\s*-\s{1,}")

PERSON = re.compile(
    r"\b(?:sri|smt|kum|dr|mr|ms)\b\.?|\bofficers?\b|\bemployees?\b"
    r"|\bengineers?\b|\bteachers?\b|\bservices?\b|\bcadre\b|\bstaff\b"
    r"|\bpersonnel\b|\bposts?\b|\bincumbent", re.I)
ACT_RULES = re.compile(r"\bact\b|\brules?\b|\bregulations?\b|\bordinance\b",
                       re.I)
GO_OBJ = re.compile(r"\bg\.?\s?o\.?s?\b|\borders?\s+issued\b|\bgovt\.? orders?\b"
                    r"|\bgovernment orders?\b", re.I)
AMOUNT = re.compile(r"\brs\.?\s?[\d,]+|\bamount of\b|\bcrores?\b|\blakhs?\b",
                    re.I)


def segments(text):
    parts = [p.strip() for p in SEG_SPLIT.split(text)]
    return [p for p in parts if p and not BOILER.match(p)]


# ---------------------------------------------------------------------------
# Rules. Order within each list = precedence. Each: (id, sub_category, regex).
# `guard(seg, full)` may veto or redirect; None keeps the rule's own target.
# ---------------------------------------------------------------------------
# Action words that IMPLY a legal instrument even when none is named in the
# segment ("Amendment - Orders - Issued" amends an earlier GO). The rest
# (extension, modification, cancellation, withdrawal) routinely act on
# leases, land use, permissions, posts -- they need an EXPLICIT GO/Act
# object or the hit is vetoed and the true subject classifies the row.
INSTRUMENT_IMPLIED = {"amendment", "repeal", "supersession"}


def amendment_sub(rid, seg, full):
    """The object decides the sub-category: 'go', 'act', or None (veto)."""
    if ACT_RULES.search(seg):
        return "act"
    if GO_OBJ.search(seg):
        return "go"
    if rid in INSTRUMENT_IMPLIED:
        if ACT_RULES.search(full):
            return "act"
        return "go"   # amending an unnamed earlier order amends a GO
    if GO_OBJ.search(full):
        return "go"
    return None       # object is a lease/land/permission/... -- not this category


R = re.compile
ACTION_RULES = [
    # --- Amendment / Modification family (object-sensitive) ---------------
    ("repeal", None, R(r"\brepeal(?:ed|ing)?\b", re.I)),
    ("supersession", None, R(r"\bsupersession\b|\bsupersed(?:ed|ing)\b", re.I)),
    ("withdrawal_go", None, R(r"\bwithdraw(?:al|n)?\b|\bwith\s?drawl\b", re.I)),
    ("cancellation", None, R(r"\bcancell?(?:ation|ed)\b|\brevocation\b|\brevoked?\b", re.I)),
    ("amendment", None, R(r"\bamendments?\b|\bamended\b", re.I)),
    ("extension_go", None, R(r"\bextension\b|\bextended\b|\bcontinuation\b|\bcontinued\b", re.I)),
    ("modification", None, R(r"\bmodifications?\b|\bmodified\b", re.I)),

    # --- Enactment / Framing ----------------------------------------------
    ("framing_rules", ("Enactment / Framing of Act & Rules", "Framing of Rules"),
     R(r"\bframing of\b.{0,40}\brules\b|\brules\b.{0,20}\bframed\b|\bmaking of rules\b", re.I)),
    ("making_regulations", ("Enactment / Framing of Act & Rules", "Making of Regulations"),
     R(r"\bmaking of regulations\b|\bregulations\b.{0,20}\bmade\b", re.I)),
    ("notify_act_rules", ("Enactment / Framing of Act & Rules", "Notification of Act / Rules"),
     R(r"\b(?:act|rules|regulations)\b[^|]{0,120}\bnotification\b|\bnotification of\b.{0,60}\b(?:act|rules)\b", re.I)),

    # --- Implementation / Effective Date ----------------------------------
    ("come_into_force", ("Implementation / Effective Date", "Commencement"),
     R(r"\bcome into force\b|\bcommencement\b|\bappointed date\b", re.I)),
    ("effective_date", ("Implementation / Effective Date", "Effective Date"),
     R(r"\beffective (?:date|from)\b|\bwith effect from\b|\bw\.e\.f\b", re.I)),
    ("enforcement", ("Implementation / Effective Date", "Enforcement"),
     R(r"\benforcement\b", re.I)),

    # --- Budget -----------------------------------------------------------
    ("reappropriation", ("Budget Release", "Re-appropriation"),
     R(r"\bre-?appropriation\b", re.I)),
    ("release_funds", ("Budget Release", "Release"),
     R(r"\brelease of\b.{0,60}(?:funds?|amount|rs\.?|grant)|\bbudget release\b|\breleased?\b.{0,40}\brs\.?\s?[\d,]", re.I)),
    ("budget_provision", ("Budget Release", "Provision of Government Funds / Budget"),
     R(r"\bbudget (?:estimates?|provision)\b|\bprovision of funds\b|\bsupplementary (?:grants?|estimates?)\b", re.I)),
    ("sanction_amount", ("Budget Release", "Sanction"),
     R(r"\bsanction(?:ed)?\b[^|]{0,90}\brs\.?\s?[\d,]|\brs\.?\s?[\d,][^|]{0,90}\bsanction(?:ed)?\b|\bgrant[- ]in[- ]aid\b|\bfinancial sanction\b", re.I)),

    # --- GPF (before generic service rules) --------------------------------
    ("gpf", None, R(r"\bg\.?p\.?f\.?\b|\bgeneral provident fund\b|\bprovident fund\b", re.I)),

    # --- Tours & Travel ----------------------------------------------------
    ("foreign_visit", ("Tours & Travel", "Foreign Visit"),
     R(r"\bforeign (?:visit|tour|travel)\b|\bex[- ]?india\b|\babroad\b|\bvisit to\b.{0,50}\b(?:usa|uk|japan|china|singapore|germany|france|dubai|malaysia)\b", re.I)),
    ("official_tour", ("Tours & Travel", "Official Tour"),
     R(r"\bofficial tour\b|\btour programme\b|\btour of\b", re.I)),
    ("travel_permission", ("Tours & Travel", "Travel Permission"),
     R(r"\bpermission to travel\b|\btravel permission\b", re.I)),

    # --- Transfers & Postings (personnel-guarded) ---------------------------
    ("transfer", ("Transfers & Postings", "Transfer"),
     R(r"\btransfers?\b(?! of)|\btransfers? and postings?\b|\btransferred\b", re.I)),
    ("transfer_of_person", ("Transfers & Postings", "Transfer"),
     R(r"\btransfers? of\b", re.I)),
    ("posting", ("Transfers & Postings", "Posting"),
     R(r"\bre-?posting\b|\bpostings?\b|\bposted\b", re.I)),
    ("relieving", ("Transfers & Postings", "Relieving"), R(r"\brelie(?:ving|ved)\b", re.I)),
    ("joining", ("Transfers & Postings", "Joining"), R(r"\bjoining\b", re.I)),
    ("deputation", ("Transfers & Postings", "Deputation"),
     R(r"\bdeputation\b|\bdeputed\b", re.I)),
    ("repatriation", ("Transfers & Postings", "Reallocation of Personnel"),
     R(r"\brepatriat(?:ion|ed)\b|\ballotment of\b.{0,40}\bcadre\b|\breallocation\b", re.I)),

    # --- Service Matters ----------------------------------------------------
    ("prosecution", ("Service Matters", "Disciplinary Matters"),
     R(r"\bsanction (?:of|for) prosecution\b|\bprosecution\b|\btrapped\b|\bacb\b|\bcorruption\b|\bdisciplinary\b|\bdepartmental (?:proceedings?|action|enquiry|inquiry)\b|\ballegations?\b|\bmisappropriation\b|\bmisconduct\b|\bbribe\b|\bcharges? framed\b|\bdismiss(?:al|ed)\b|\bremoval from service\b|\bcensure\b|\bpenalt(?:y|ies)\b", re.I)),
    ("suspension_person", ("Service Matters", "Disciplinary Matters"),
     R(r"\bsuspension\b|\bsuspended\b", re.I)),
    ("compassionate", ("Service Matters", "Appointment"),
     R(r"\bcompassionate\b", re.I)),
    ("recruitment", ("Service Matters", "Recruitment"),
     R(r"\brecruitment\b|\bfilling (?:up )?(?:of )?(?:the )?(?:vacant )?posts?\b", re.I)),
    ("posts_establishment", ("Service Matters", "Posts & Establishment"),
     R(r"\b(?:creation|sanction|continuation|abolition|up-?gradation|conversion|shifting|revival)\b[^|]{0,60}\bposts?\b|\bposts?\b[^|]{0,40}\b(?:created|sanctioned|continued|abolished|upgraded|revived)\b|\bstaffing pattern\b|\bcadre strength\b", re.I)),
    ("promotion", ("Service Matters", "Promotion"),
     R(r"\bpromotions?\b|\bpromoted\b|\bpanel (?:of|for)\b|\bpanel year\b|\binclusion of\b.{0,70}\bpanel\b", re.I)),
    ("appointment", ("Service Matters", "Appointment"),
     R(r"\bappointments?\b|\bappointed\b|\bnotional appointment\b", re.I)),
    ("seniority", ("Service Matters", "Seniority"),
     R(r"\bseniority\b|\bgradation list\b", re.I)),
    ("pension", ("Service Matters", "Pension"),
     R(r"\bpensions?\b|\bpensionary\b|\bfamily pension\b|\bcommutation\b", re.I)),
    ("retirement", ("Service Matters", "Retirement"),
     R(r"\bretirement\b|\bsuperannuation\b|\bretired?\b on attaining", re.I)),
    ("leave", ("Service Matters", "Leave"),
     R(r"\bleave\b", re.I)),
    ("pay_allowances", ("Service Matters", "Pay & Allowances"),
     R(r"\bpay (?:scales?|fixation|revision)\b|\ballowances?\b|\bdearness allowance\b|\bd\.a\b|\bincrements?\b|\bhra\b|\bpay & allowances\b|\bprc\b", re.I)),
    ("regularization", ("Service Matters", "Service Conditions"),
     R(r"\bregulari[sz](?:ation|ed)\b", re.I)),
    ("service_conditions", ("Service Matters", "Service Conditions"),
     R(r"\bservice (?:conditions|rules)\b|\bprobation\b|\bautomatic advancement\b", re.I)),
    ("loans_advances", ("Service Matters", "Pay & Allowances"),
     R(r"\bhouse building advance\b|\bloans and advances\b|\badvance of rs\b|\bfestival advance\b", re.I)),

    # --- Introduction / Formation -------------------------------------------
    ("constitution_committee", ("Introduction / Formation", "Constitution of Committee"),
     R(r"\b(?:re-?)?constitut(?:ion|ed|ing)\b[^|]{0,80}\b(?:committee|commission|board|council|task force)\b|\b(?:committee|commission|task force)\b[^|]{0,50}\bconstitut", re.I)),
    ("formation_authority", ("Introduction / Formation", "Formation of Authority"),
     R(r"\b(?:formation|constitution|creation)\b[^|]{0,70}\bauthority\b|\bauthority\b[^|]{0,40}\b(?:formation|constituted)\b", re.I)),
    ("establishment_institution", ("Introduction / Formation", "Establishment of Institution"),
     R(r"\bestablishment of\b[^|]{0,80}\b(?:university|college|school|institute|institution|hospital|centre|center|academy)\b|\bsetting up of\b", re.I)),
    ("creation_body", ("Introduction / Formation", "Creation of Body"),
     R(r"\b(?:formation|creation|constitution)\b[^|]{0,70}\b(?:corporation|society|board|body|mandal|district|gram panchayat|municipality|division|zone)\b", re.I)),
    ("new_policy", ("Introduction / Formation", "Introduction of Policy"),
     R(r"\b(?:new|introduction of(?: a)?)\b[^|]{0,40}\bpolicy\b|\bpolicy\b[^|]{0,30}\b(?:20\d\d|announced|introduced)\b", re.I)),

    # --- Scheme / Programme -------------------------------------------------
    ("scheme_guidelines", ("Scheme / Programme", "Scheme Guidelines"),
     R(r"\b(?:scheme|programme|mission)\b[^|]{0,90}\bguidelines\b|\bguidelines\b[^|]{0,70}\b(?:scheme|programme|mission|implementation)\b", re.I)),
    ("scheme_implementation", ("Scheme / Programme", "Scheme Implementation"),
     R(r"\bimplementation of\b[^|]{0,70}\b(?:scheme|programme|mission)\b|\b(?:scheme|programme|mission)\b[^|]{0,50}\bimplementation\b", re.I)),
    ("subsidy", ("Scheme / Programme", "Subsidy"), R(r"\bsubsid(?:y|ies|ised|ized)\b", re.I)),
    ("financial_assistance", ("Scheme / Programme", "Financial Assistance"),
     R(r"\bfinancial assistance\b|\bex[- ]?gratia\b", re.I)),

    # --- Land & Property Matters (user-approved addition 2026-09-19) --------
    ("land_acquisition", ("Land & Property Matters", "Land Acquisition"),
     R(r"\bland acquisition\b|\bacquisition of\b[^|]{0,70}\bland\b|\bacquir(?:e|ing|ed)\b[^|]{0,50}\bland\b|\bl\.?a\.? ?act\b", re.I)),
    ("land_allotment", ("Land & Property Matters", "Land Allotment / Alienation / Assignment"),
     R(r"\b(?:allotment|alienation|assignment|lease)\b[^|]{0,80}\bland\b|\bland\b[^|]{0,60}\b(?:allotment|alienation|assignment|allotted|alienated|assigned|leased?)\b|\balienation of\b[^|]{0,60}\b(?:acres?|ac\.|site|extent)\b|\ballotment of\b[^|]{0,50}\b(?:acres?|ac\.|site|extent)\b", re.I)),
    ("land_transfer", ("Land & Property Matters", "Land Transfer / Resumption"),
     R(r"\btransfer of\b[^|]{0,70}\bland\b|\bland\b[^|]{0,60}\b(?:resumption|resumed)\b|\bresumption of\b[^|]{0,60}\b(?:land|site|acres?)\b", re.I)),
    ("urban_planning", ("Land & Property Matters", "Urban Planning & Land Use"),
     R(r"\bmaster plan\b|\bland use\b|\bzon(?:e|al|ing) regulations?\b|\bchange of land use\b|\blay-?outs?\b|\bbuilding penali[sz]ation\b|\bb\.?p\.?s\b|\bl\.?r\.?s\b|\btown planning\b|\burban development authorit(?:y|ies)\b", re.I)),
    ("govt_accommodation", ("Land & Property Matters", "Government Accommodation / Quarters"),
     R(r"\ballotment of\b[^|]{0,60}\b(?:quarters?|accommodation|house)\b|\b(?:quarters?|accommodation)\b[^|]{0,50}\ballot(?:ment|ted)\b|\bgovernment accommodation\b|\brent[- ]free accommodation\b|\beviction\b[^|]{0,50}\bquarters?\b", re.I)),

    # --- Works & Projects (user-approved addition 2026-09-19) ---------------
    ("works_approval", ("Works & Projects", "Administrative Approval of Works"),
     R(r"\badministrative (?:sanction|approval)\b[^|]{0,90}\b(?:works?|construction|road|bridge|building|project)\b|\b(?:works?|construction|project)\b[^|]{0,60}\badministrative (?:sanction|approval)\b|\brevised administrative sanction\b", re.I)),
    ("tenders", ("Works & Projects", "Tenders / Entrustment"),
     R(r"\btenders?\b|\bentrust(?:ment|ed|ing)?\b[^|]{0,70}\b(?:works?|construction|project|procur)|\b(?:works?|construction|procurement)\b[^|]{0,60}\bentrust|\bnomination basis\b", re.I)),
    ("project_execution", ("Works & Projects", "Project Execution"),
     R(r"\bexecution of\b[^|]{0,60}\bworks?\b|\btaking up\b[^|]{0,50}\bworks?\b|\bconstruction of\b|\bwidening of\b|\bimprovements? to\b[^|]{0,40}\b(?:road|canal|tank)\b", re.I)),

    # --- Mines & Minerals (user-approved addition 2026-09-19; before the
    # --- generic licence rules so "prospecting licence" lands here) ---------
    ("mining_lease", ("Mines & Minerals", "Mining Lease"),
     R(r"\bmining leases?\b|\bmining rights\b|\bmining leasehold\b", re.I)),
    ("quarry_lease", ("Mines & Minerals", "Quarry Lease"),
     R(r"\bquarry(?:ing)? leases?\b|\bquarry leasehold\b", re.I)),
    ("mineral_concession", ("Mines & Minerals", "Mineral Concessions"),
     R(r"\bmineral concessions?\b|\bprospecting licen[cs]es?\b|\bcomposite licen[cs]e\b|\bse(?:ig|g)niorage\b", re.I)),
    ("mines_general", ("Mines & Minerals", "Other Mines & Minerals Matters"),
     R(r"\bmines? (?:&|and) minerals?\b|\b(?:minor|major) minerals?\b|\bmining\b|\bquarr(?:y|ies)\b", re.I)),

    # --- Licences, Permissions & Exemptions (user-approved 2026-09-19;
    # --- travel_permission is earlier in this list and wins its segments) ---
    ("exemption", ("Licences, Permissions & Exemptions", "Exemption / Relaxation"),
     R(r"\bexempt(?:ion|ed|ing)s?\b|\brelaxation\b|\brelaxed\b", re.I)),
    ("licence", ("Licences, Permissions & Exemptions", "Licence"),
     R(r"\blicen[cs]es?\b|\blicen[cs]ing\b", re.I)),
    ("permission_noc", ("Licences, Permissions & Exemptions", "Permission / NOC"),
     R(r"\bpermission\b|\bno objection certificate\b|\bnoc\b|\bpermitted\b", re.I)),

    # --- eOffice / Administrative Processing --------------------------------
    ("admin_sanction", ("eOffice / Administrative Processing", "Administrative Approval"),
     R(r"\badministrative (?:sanction|approval)\b", re.I)),
    ("delegation_powers", ("eOffice / Administrative Processing", "Administrative Instructions"),
     R(r"\bdelegation of\b.{0,40}\bpowers\b", re.I)),
    ("admin_instructions", ("eOffice / Administrative Processing", "Administrative Instructions"),
     R(r"\binstructions\b|\bprocedure\b|\bprocedural\b|\bclarifications?\b", re.I)),
    ("implementation_generic", ("Implementation / Effective Date", "Implementation"),
     R(r"\bimplementation\b|\boperationali[sz]ation\b", re.I)),
]

# subject rules: never primary on their own unless nothing else fires;
# their main job is secondary tags.
# (id, (category, sub), regex, tag_ok) -- tag_ok=False means the rule may
# serve as last-resort primary but never adds a secondary tag (an Act being
# MENTIONED does not make a GO an Amendment).
SUBJECT_RULES = [
    ("scheme_subject", ("Scheme / Programme", "Government Programme"),
     R(r"\bschemes?\b|\bprogrammes?\b|\bmission\b|\bnavaratnalu\b|\bpathakam\b|\byojana\b", re.I), True),
    ("welfare_subject", ("Scheme / Programme", "Welfare Scheme"),
     R(r"\bwelfare\b", re.I), True),
    ("budget_subject", ("Budget Release", "Other Budget Matters"),
     R(r"\bbudget\b|\bfunds?\b|\bexpenditure\b", re.I), True),
    ("service_subject", ("Service Matters", "Other Service Matters"),
     R(r"\bpublic serv(?:ants?|ices?)\b|\bservice matters\b|\bestablishment\b", re.I), True),
    ("land_subject", ("Land & Property Matters", "Other Land & Property Matters"),
     R(r"\blands?\b|\bsy\.? ?nos?\b|\bsurvey num", re.I), True),
    ("act_subject", ("Amendment / Modification", "Other Legislative / GO Changes"),
     R(r"\bact,?\s*(?:19|20)\d\d\b|\brules,?\s*(?:19|20)\d\d\b", re.I), False),
]

AMENDMENT_MAP = {
    # rule id            (sub when object=GO,                sub when object=Act/Rules)
    "amendment":     ("Amendment of GO", "Amendment of Act / Rules"),
    "repeal":        ("Revocation / Cancellation of GO", "Revocation / Repeal of Act / Rules"),
    "cancellation":  ("Revocation / Cancellation of GO", "Revocation / Repeal of Act / Rules"),
    "withdrawal_go": ("Withdrawal of GO", "Revocation / Repeal of Act / Rules"),
    "supersession":  ("Supersession of GO", "Other Legislative / GO Changes"),
    "extension_go":  ("Extension / Continuation of GO", "Other Legislative / GO Changes"),
    "modification":  ("Modification of GO", "Amendment of Act / Rules"),
}
AMENDMENT_IDS = set(AMENDMENT_MAP)


def resolve_amendment(rid, seg, full):
    which = amendment_sub(rid, seg, full)
    if which is None:
        return None
    go_sub, act_sub = AMENDMENT_MAP[rid]
    return ("Amendment / Modification", act_sub if which == "act" else go_sub)


def resolve_gpf(seg, full):
    s = seg + " " + full
    for sub, rx in (("GPF Advance", r"\badvance\b"),
                    ("GPF Withdrawal", r"\bwithdrawal\b"),
                    ("GPF Final Settlement", r"\bfinal (?:settlement|payment)\b"),
                    ("GPF Nomination", r"\bnomination\b"),
                    ("GPF Subscription", r"\bsubscription\b")):
        if re.search(rx, s, re.I):
            return ("General Provident Fund (GPF)", sub)
    return ("General Provident Fund (GPF)", "Other GPF Matters")


STATUTE_NAME_AFTER = re.compile(r"^\s*\)?\s*(?:act|rules|regulations)\b", re.I)


def rule_target(rid, target, seg, full, m):
    """Resolve a rule hit to (category, sub) or None to veto the hit."""
    if rid in AMENDMENT_IDS:
        # "...(Amendment) Act, 2018" names a statute; it is a citation,
        # not this GO's action -- veto
        if STATUTE_NAME_AFTER.match(seg[m.end():]):
            return None
        return resolve_amendment(rid, seg, full)
    if rid == "gpf":
        return resolve_gpf(seg, full)
    if rid in ("transfer", "transfer_of_person", "posting", "relieving",
               "joining", "repatriation"):
        # personnel guard: transfer/posting of assets, roads, land,
        # institutions is NOT Transfers & Postings
        window = seg
        if rid == "transfer_of_person":
            m = re.search(r"\btransfers? of\b(.{0,60})", seg, re.I)
            window = m.group(1) if m else seg
        if not PERSON.search(window):
            return None
    if rid == "land_acquisition" and re.search(
            r"\bland acquisition (?:units?|officers?|establishment|wing)\b"
            r"|\bo/o\b|\bspecial (?:deputy )?collector\b", seg, re.I):
        return None   # "Land Acquisition" here is an office's NAME
    if rid == "project_execution" and re.search(
            r"\bhouse building advance\b|\bh\.?b\.?a\b", full, re.I):
        return None   # the construction is what an HBA loan is FOR
    if rid == "works_approval" and re.search(
            r"\bbudget release orders?\b|\bb\.?r\.?o\b", full, re.I):
        return None   # a BRO's operative action is the release, works is context
    if rid == "permission_noc" and re.search(
            r"\bvoluntary retirement\b|\bresignation\b", full, re.I):
        return None   # permission is the vehicle; retirement is the substance
    if rid == "regularization" and re.search(
            r"\blay-?outs?\b|\bplots?\b|\bconstructions?\b|\bbuildings?\b"
            r"|\bland\b|\bencroach", seg, re.I):
        # regularisation of layouts/plots/constructions is land-use,
        # not a service matter
        return ("Land & Property Matters", "Urban Planning & Land Use")
    if rid == "suspension_person" and GO_OBJ.search(seg):
        return ("Amendment / Modification", "Suspension of GO")
    if rid == "pension" and re.search(
            r"\bdepartmental (?:proceedings?|action|enquiry|inquiry)\b"
            r"|\ballegations?\b|\bprosecution\b|\bmisappropriation\b"
            r"|\btrapped\b|\bacb\b|\bcorruption\b|\bdisciplinary\b"
            r"|\brule 9 of\b.{0,60}\bpension rules\b", full, re.I):
        # a Rule-9 pension cut is a penalty: the GO is disciplinary, the
        # pension is what the penalty acts on
        return ("Service Matters", "Disciplinary Matters")
    if rid == "leave" and re.search(r"\bex[- ]?india\b", seg + " " + full, re.I):
        return ("Tours & Travel", "Foreign Visit")
    return target


def classify(text):
    """Pure function: text -> dict. No state, no neighbours."""
    text = " ".join(text.split())
    segs = segments(text)
    if not segs:
        # text exists but is all boilerplate/rulers -- flagged, not guessed,
        # and kept in Others so category-is-null means exactly no_text
        return {"go_category": "Others", "go_sub_category": None,
                "secondary_tags": [], "status": "no_rule_matched",
                "evidence": text[:160] or None, "rule": None}

    hits = []   # (seg_idx, rule_order, rid, category, sub, matched_text, seg)
    for si, seg in enumerate(segs):
        for order, (rid, target, rx) in enumerate(ACTION_RULES):
            m = rx.search(seg)
            if not m:
                continue
            resolved = rule_target(rid, target, seg, text, m)
            if resolved is None:
                continue
            cat, sub = resolved
            hits.append((si, order, rid, cat, sub, m.group(0), seg))

    primary = None
    if hits:
        # the operative action is the LAST action segment; within a segment,
        # rule order is precedence
        hits.sort(key=lambda h: (-h[0], h[1]))
        primary = hits[0]

    # secondary tags: every OTHER (category, sub) an action rule matched,
    # plus subject-rule categories -- "Category; Sub" strings, deduped,
    # never duplicating the primary
    tags, seen = [], set()
    if primary:
        seen.add((primary[3], primary[4]))
        for h in hits[1:]:
            key = (h[3], h[4])
            if key in seen:
                continue
            seen.add(key)
            tags.append(f"{h[3]}; {h[4]}")
    subject_hit = None
    tagged_cats = {k[0] for k in seen}
    for rid, (cat, sub), rx, tag_ok in SUBJECT_RULES:
        m = rx.search(text)
        if not m:
            continue
        if subject_hit is None:
            subject_hit = (rid, cat, sub, m.group(0))
        if tag_ok and primary and cat not in tagged_cats:
            tags.append(cat)
            tagged_cats.add(cat)

    if primary:
        return {"go_category": primary[3], "go_sub_category": primary[4],
                "secondary_tags": tags[:4],
                "status": "classified",
                "evidence": primary[6][:160], "rule": primary[2]}
    if subject_hit:
        rid, cat, sub, ev = subject_hit
        return {"go_category": cat, "go_sub_category": sub,
                "secondary_tags": [], "status": "classified_subject_only",
                "evidence": ev[:160], "rule": rid}
    return {"go_category": "Others", "go_sub_category": None,
            "secondary_tags": [], "status": "no_rule_matched",
            "evidence": text[:160], "rule": None}


# ---------------------------------------------------------------------------
def load_corpus():
    for f in sorted(Path("out/corpus").glob("[12]*.jsonl")):
        for line in f.open():
            line = line.strip()
            if line:
                yield f.stem, json.loads(line)


def classify_record(rec):
    abstract = (rec.get("abstract") or "").strip()
    order_text = (rec.get("order_text") or "").strip()
    if abstract:
        row = classify(abstract)
        row["basis"] = "abstract"
    elif order_text:
        row = classify(order_text[:600])
        row["basis"] = "order_text_head"
    else:
        row = {"go_category": None, "go_sub_category": None,
               "secondary_tags": [], "status": "no_text_to_classify",
               "evidence": None, "rule": None, "basis": None}
    row["source_file"] = rec["source_file"]
    row["filename"] = rec["filename"]
    return row


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", type=int, default=0)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()

    if args.sample:
        recs = [r for _s, r in load_corpus()]
        random.seed(args.seed)
        picks = random.sample(recs, args.sample)
        c = Counter()
        shown = 0
        for rec in picks:
            row = classify_record(rec)
            c[(row["go_category"], row["go_sub_category"])] += 1
            if shown < 40 and row["basis"] == "abstract":
                a = " ".join((rec.get("abstract") or "").split())
                print(f"ABSTRACT: {a[:200]}")
                print(f"    -> {row['go_category']} > {row['go_sub_category']}"
                      f"   [{row['status']}, rule={row['rule']}, ev={row['evidence']!r}]")
                if row["secondary_tags"]:
                    print(f"    tags: {row['secondary_tags']}")
                print()
                shown += 1
        print("=" * 76)
        for (cat, sub), v in c.most_common():
            print(f"  {v:>5}  {cat} > {sub}")
        return

    if not args.apply:
        print("dry run over the whole corpus (no writes); pass --apply to write")

    by_shard = {}
    stat = Counter()
    catc = Counter()
    for shard, rec in load_corpus():
        row = classify_record(rec)
        stat[row["status"]] += 1
        catc[row["go_category"]] += 1
        by_shard.setdefault(shard, []).append(row)

    if args.apply:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        n = 0
        for shard, rows in sorted(by_shard.items()):
            with (OUT_DIR / f"{shard}.jsonl").open("w") as fh:
                for r in rows:
                    fh.write(json.dumps(r, ensure_ascii=False) + "\n")
                    n += 1
        print(f"wrote {n} rows to {OUT_DIR}/")

    print("\nstatus:")
    for k, v in stat.most_common():
        print(f"  {v:>6}  {k}")
    print("\ngo_category:")
    for k, v in catc.most_common():
        print(f"  {v:>6}  {k}")


if __name__ == "__main__":
    main()
