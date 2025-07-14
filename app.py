# app.py

# # Copyright (c) 2025, Eliot D. Williams
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.

# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.

# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

import os, requests, threading
import re
import time
import tarfile
import tempfile
import ocrmypdf
import shutil
import subprocess
import json
import csv
from io import StringIO
from requests.exceptions import RequestException, Timeout, HTTPError
from dateutil import parser  
from datetime import datetime          
from playwright.sync_api import sync_playwright
from datetime import datetime
from flask import Flask, render_template, request, Response, stream_with_context, send_file, redirect, url_for
from werkzeug.http import parse_options_header

app = Flask(__name__)
try:
    with open(os.path.join(os.path.dirname(__file__), "API_KEY.txt")) as f:
        API_KEY = f.read().strip()
except Exception as e:
    raise RuntimeError(f"Failed to load API_KEY.txt: {e}")

SEARCH_URL  = "https://api.uspto.gov/api/v1/patent/applications/search"


PDF_CACHE_DIR = "uspto_pdf_cache"
os.makedirs(PDF_CACHE_DIR, exist_ok=True)

#MAIN logic to populate index.html
@app.route("/", methods=["GET", "POST"])
def home():
    #print("Home running")
    
    #PARSE THE DATA FROM THE HTML PAGE, IF ANY RECEIVED YET
    #Search box input
    search_term = request.values.get("search_term", None)
    search_term = search_term.strip() if search_term else ""

    #URL arguments
    # confirm_large → Indicates user consent to load large results    
    application_number = request.args.get("application_number", None)
    application_number = application_number.strip() if application_number else ""

    patent_number = request.args.get("patent_number", None)
    patent_number = patent_number.strip() if patent_number else ""

    publication_number = request.args.get("publication_number", None)
    publication_number = publication_number.strip() if publication_number else ""

    proceeding_number = request.args.get("proceeding_number", None)
    proceeding_number = proceeding_number.strip() if proceeding_number else ""

    #This gets set from the confirm_large_results.html page
    confirm_large = request.form.get("confirm_large", "").lower() == "true"
    
    
    # Results - search results from query to PTO search API
    # Proceedings - PTAB proceeding list
    # Documents - Docket entries from a PTAB proceeding
    #
    # Initializes variables used later to populate the template:
    # events, results, documents, proceedings → All start empty and are filled conditionally depending on what kind of result the search returns.
    # preview_warning → Becomes True if a large search is detected and user hasn’t confirmed they want all results.
    # error → Stores any error messages to show in the template.
    # patent_info → Set to a dictionary with patent details if a match is found.
    # total → Holds count of matching results (to conditionally show preview).
    events = []
    results = []
    documents = []
    proceedings = []
    preview_warning = False
    error = None
    patent_info = None
    family_members = []
    total = 0

    ptab_patent_number = ""
    ptab_po = ""
    ptab_application_number = ""
    id_to_try = ""


    # If patent_info remains None, the "Patent Details" section won’t render.
    # If results is empty, the results table is skipped.
    # If documents or proceedings aren't populated, their tables are skipped.

   
    try:
        # ─── 1: IF [letters]YYYY-xxxx, or a PTAB docket # was clicked, get PTAB DOCUMENTS and return───        
        if re.match(r"^[A-Za-z]+\d{4}-\d+$", search_term) or proceeding_number:
            proceeding_number = proceeding_number or search_term
            template_name, t_args = ptab_structured_search(proceeding_number)            
            return render_template(template_name, **t_args)
            
        # ─── 2: IF search box and not PTAB Docket  #, do unstructured search box processing───
        elif search_term:
            template_name, t_args = unstructured_search(search_term, confirm_large)
            return render_template(template_name, **t_args)                
        
        # ───3: If non PTAB proceeding URLs were clicked, do the appropriate STRUCTURED QUERY ───
        #print(f"Entered structured query")
        else:
            q = ""
            if publication_number:
                pub = publication_number.replace(" ", "").replace("/", "")
                if pub.upper().startswith("WO"):
                    q = f"publicationNumberText:{pub}"
                else:
                    q = f"applicationMetaData.earliestPublicationNumber:{pub}"
            elif application_number:
                id_to_try = application_number
                if re.match(r"^PCT/[A-Z]{2}\d{4}/\d{6}$", application_number, re.IGNORECASE):
                    q = f"applicationMetaData.pctPublicationNumber:{application_number}"
                else:
                    q = f"applicationNumberText:{application_number}"
            elif patent_number:
                id_to_try = patent_number
                #print(f"Entered patent number query with {patent_number}")
                q = f"applicationMetaData.patentNumber:{patent_number}"
        
            if q:
                #print(f"q block entered with {q}")
                pfws = []
                try:
                    #print(f"Trying to fetch pages using {q}")
                    try:
                        total, pfws = fetch_all_pages(q)
                        #print(f"Fetched with total: {total}")
                    except ValueError as e:
                        print(f"⚠️ USPTO fetch failed with error: {e}, trying PTAB fallback")
                        error = f"USPTO lookup failed: {e}"
                        try:
                            proceedings = search_ptab_by_id(id_to_try)
                        except Exception as ptab_e:
                            print(f"❌ PTAB fallback also failed: {ptab_e}")
                            error = f"USPTO and PTAB lookup both failed: {ptab_e}"

                    if total > 0:
                        try:
                            #print(f"Trying to extract from {pfws[0]}")
                            patent_info, events, proceedings = extract_patent_details(pfws[0])
                            #print(f"Returned from extract")

                            # 🧬 Recursively build family tree
                            #print("Calling gather_family_tree now:")
                            family_tree = gather_family_tree(patent_info.get("application_number"))


                            family_members = []
                            for app_no in family_tree:
                                if app_no == patent_info.get("application_number"):
                                    continue  # Skip self

                                try:
                                    print(f"Fetching pages for {app_no}")
                                    total, pfws = fetch_all_pages(f"applicationNumberText:{app_no}", limit=1)

                                    # Defensive checks
                                    if pfws and isinstance(pfws, list) and isinstance(pfws[0], dict):
                                        pfw = pfws[0]
                                        patent_info_entry, _, _ = extract_patent_details(pfw)
                                        family_members.append({
                                            "application_number": app_no,
                                            "patent_number": patent_info_entry.get("patent_number", ""),
                                            "title": patent_info_entry.get("title", "(No Title)"),
                                            "filing_date": patent_info_entry.get("filing_date", "—"),
                                        })
                                    else:
                                        print(f"⚠️ No valid PFW data returned for {app_no}: pfws={pfws}")
                                except Exception as e:
                                    print(f"⚠️ Could not extract details for {app_no}: {e}")

                          
                  

                        except Exception as e:
                            print(f"Failed to extract details from USPTO hit: {e}")
                            
                    else:
                        print(f"Lookup failed for: {q}")
                        error = f"No USPTO data found for query: {q}"
                        try:
                            proceedings = search_ptab_by_id(id_to_try)
                        except: 
                            pass
                    
                    # If extract failed or patent_info is missing key info, fill from PTAB if available
                    # If we got proceedings, extract PTAB metadata in case needed:                    
                    if proceedings:
                        ptab_patent_number = proceedings[0].get("ptab_patent_number")
                        ptab_po = proceedings[0].get("ptab_patent_owner")
                        ptab_application_number = proceedings[0].get("ptab_application_number")
                    
                    if not patent_info:
                        patent_info = {}

                    if not patent_info.get("patent_number") and ptab_patent_number:
                        patent_info["patent_number"] = ptab_patent_number

                    if not patent_info.get("application_number") and ptab_application_number:
                        patent_info["application_number"] = ptab_application_number

                    if ptab_po:
                        assignees = patent_info.setdefault("assignees", [])
                        if not any(a.get("name") == ptab_po for a in assignees):
                            assignees.append({
                                "name": ptab_po,
                                "pdf_url": ""
                            })                                    
                except Exception as e:
                    print(f"🔴 All searches failed: {e}")
                    error = f"USPTO and PTAB lookup both failed: {e}"
    except Exception as e:
        print(f"🔴 All searches failed on outer code: {e}")
        error = f"All searches failed: {e}"

    if family_members:
        family_members = sort_family_members(family_members)
     
    
    return render_template(
        "index.html",
        search_term=search_term,
        application_number=application_number,
        patent_number=patent_number,
        publication_number=publication_number,
        proceeding_number=proceeding_number,
        documents=documents,
        patent=patent_info,
        family_members=family_members,
        events=events,
        proceedings=proceedings,
        results=results,
        error=error,
        total_results=total,
    )

# Runs the search logic for the search box.  Called only if the search_term wasn't in
# a PTAB docket number form like IPR2014-00342
# returns template info and template arguments to home to render
def unstructured_search(search_term, confirm_large):
    # ───  SEARCH BOX FLOW IF WE DON'T KNOW WHAT THE USER IS SEARCHING FOR ───
    search_term = search_term.strip() 
    #print(f"running search term processing on {search_term}")
    ptab_patent_number =""
    ptab_po = ""
    ptab_application_number = ""
    application_number = ""
    patent_number = ""
    publication_number = ""
    proceeding_number = ""
    documents = []
    patent_info = None
    family_members = []
    events = []
    proceedings = []
    results = []
    error = None
    total = 0


    try:
        fields = [
            "assignmentBag.assigneeBag.assigneeNameText",
            "applicationNumberText",
            "applicationMetaData.filingDate",
            "applicationMetaData.pctPublicationNumber",                        
            "applicationMetaData.applicationStatusDescriptionText",
            "applicationMetaData.inventionTitle",
            "applicationMetaData.patentNumber"
        ]
        # Determine result cap based on whether user has confirmed they want all results
        limit = None if confirm_large else 1000

        # Fetch results: either capped at 1000 or full set if confirmed
        total, pfws = fetch_all_pages(
            search_term,
            fields=fields,
            limit=limit
        )

        # If more than 1000 total and user hasn't confirmed, show preview + confirm prompt
        if total > 1000 and not confirm_large:
            return "confirm_large_results.html", {
                "total": total,
                "preview": pfws[:100],
                "search_term": search_term
            }

        
        # If we narrowed down to single (or no) hit, try to get PTAB info
        if total < 2:
            m = re.match(r"^\d{7,8}$", search_term)
            if m:
                id_to_try = m.group(0)                
                try:
                    proceedings = search_ptab_by_id(id_to_try)
                    # If we got proceedings, extract PTAB metadata in case needed:
                    if proceedings:
                        ptab_patent_number = proceedings[0].get("ptab_patent_number")
                        ptab_po = proceedings[0].get("ptab_patent_owner")
                        ptab_application_number = proceedings[0].get("ptab_application_number")
                except Exception:
                    # Optionally log or assign an error message here
                    pass

            # Only one hit, so try to display it as a details page
            if total == 1:
                pfw = pfws[0]
                patent_info, events, _ = extract_patent_details(pfw)
                print("running family tree")
                family_tree = gather_family_tree(patent_info.get("application_number"))
                print("returned from family tree call")
                
                family_members = []
                for app_no in family_tree:
                    if app_no == patent_info.get("application_number"):
                        continue  # Skip self

                    try:
                        print(f"Fetching pages for {app_no}")
                        total, pfws = fetch_all_pages(f"applicationNumberText:{app_no}", limit=1)

                        # Defensive checks
                        if pfws and isinstance(pfws, list) and isinstance(pfws[0], dict):
                            pfw = pfws[0]
                            patent_info_entry, _, _ = extract_patent_details(pfw)
                            family_members.append({
                                "application_number": app_no,
                                "patent_number": patent_info_entry.get("patent_number", ""),
                                "title": patent_info_entry.get("title", "(No Title)"),
                                "filing_date": patent_info_entry.get("filing_date", "—"),
                            })
                        else:
                            print(f"⚠️ No valid PFW data returned for {app_no}: pfws={pfws}")
                    except Exception as e:
                        print(f"⚠️ Could not extract details for {app_no}: {e}")


  
            # If extract failed or patent_info is missing key info, fill from PTAB if available
            if not patent_info:
                patent_info = {}

            if not patent_info.get("patent_number") and ptab_patent_number:
                patent_info["patent_number"] = ptab_patent_number

            if not patent_info.get("application_number") and ptab_application_number:
                patent_info["application_number"] = ptab_application_number

            if ptab_po:
                assignees = patent_info.setdefault("assignees", [])
                if not any(a.get("name") == ptab_po for a in assignees):
                    assignees.append({
                        "name": ptab_po,
                        "pdf_url": ""
                    })


        #Populate results table if more than 1 entry
        else:            
            #print("Running table gen loop")
            patent_info = None
            for pfw in pfws:
                meta = pfw.get("applicationMetaData", {})
                assignees = ", ".join(
                    a.get("assigneeNameText", "")
                    for assign in pfw.get("assignmentBag", [])
                    for a in assign.get("assigneeBag", [])
                    if a.get("assigneeNameText")
                )
                results.append({
                    "application_number": pfw.get("applicationNumberText"),
                    "patent_number": meta.get("patentNumber"),
                    "filing_date": meta.get("filingDate"),
                    "status": meta.get("applicationStatusDescriptionText"),
                    "title": meta.get("inventionTitle"),
                    "assignees": assignees
                })
            
            if total < 20:
                seen_proceedings = set()
                try:
                    proceeding_hits = []

                    # Try patent number first
                    pat_no = meta.get("patentNumber")
                    if pat_no:
                        proceeding_hits = search_ptab_by_id(pat_no)

                    # If no hits, try application number
                    if not proceeding_hits:
                        app_no = pfw.get("applicationNumberText")
                        if app_no:
                            proceeding_hits = search_ptab_by_id(app_no)

                    for proc in proceeding_hits:
                        proc_num = proc.get("number")
                        if proc_num and proc_num not in seen_proceedings:
                            seen_proceedings.add(proc_num)
                            proceedings.append(proc)

                except Exception as e:
                    print(f"⚠️ PTAB lookup failed for {pfw.get('applicationNumberText')} or {meta.get('patentNumber')}: {e}")


        fallback_hits = set(p.get("number") for p in proceedings if "number" in p)
        for field in ["patentOwnerName", "partyName"]:
            extra_hits = search_ptab_by_id(search_term, all=True)
            for proc in extra_hits:
                proc_num = proc.get("number")
                if proc_num and proc_num not in fallback_hits:
                    fallback_hits.add(proc_num)
                    proceedings.append(proc)

    except RateLimitExceeded:
        error = "USPTO API rate limit reached. Please try again later."
    except Exception as e:
        error = f"Unexpected error: {e}"
    if family_members:
        family_members = sort_family_members(family_members)

    return "index.html", {
        "search_term": search_term,
        "application_number": "",
        "patent_number": "",
        "publication_number": "",
        "proceeding_number": "",
        "documents": documents,
        "patent": patent_info,
        "family_members": family_members,
        "events": events,
        "proceedings": proceedings,
        "results": results,
        "error": error,
        "total_results": total if search_term and total > 1 else None,
    }

# Takes a PTAB docket # and returns any documents found for that matter
# as well as the proceedings info that search_ptab_by_id returns to get biblio info if needed
def ptab_structured_search(proceeding_number):
    try:
        documents = get_ptab_documents(proceeding_number)

        proceedings = search_ptab_by_id(proceeding_number)

        documents.sort(
            key=lambda d: (
                -parse_date(d.get("filing_date", "")).timestamp(),  # Newest date first
                parse_doc_number(d.get("document_number", ""))      # Lowest doc number first
            )
        )

        return "index.html", {
            "search_term": proceeding_number,
            "application_number": "",
            "patent_number": "",
            "publication_number": "",
            "proceeding_number": proceeding_number,
            "documents": documents,
            "results": [],
            "patent": None,
            "events": [],
            "proceedings": proceedings,
            "error": None,
            "preview_warning": False,
            "total_results": None,
        }

    except Exception as e:
        return  "index.html", {
            "search_term": proceeding_number,
            "application_number": "",
            "patent_number": "",
            "publication_number": "",
            "proceeding_number": "",
            "documents": [],
            "results": [],
            "patent": None,
            "events": [],
            "proceedings": [],
            "error": f"Error loading PTAB data for {proceeding_number}: {e}",
            "preview_warning": False,
            "total_results": None,
        }

#Parses pfw (usually obtained by fetch all pages) when passed to extract patent info, 
#Returns patent_info, events, proceedings
    #patent_info: all biblio info, assignments, parents and child apps
    #events: prosecution events in search results
    #proceedings: list of PTAB proceedings (if a patent# or app# is in pwf)
def extract_patent_details(pfw):
    #print(f"Started extract")
    meta = pfw.get("applicationMetaData", {})
    #print(f"Got applicationMetaData")

    app_type = meta.get("applicationTypeCategory", "").upper()
    is_pct = app_type == "PCT"
    is_reexam = app_type == "REEXAM"

    # Determine patent number (may be missing for pre-grant apps)
    #TODO: check pct format
    patent_number = (
        meta.get("patentNumber") or
        meta.get("pctPublicationNumber") or
        "Patent # not found"
    )
    #print(f"Got patent#: {patent_number}")

    application_number = (
        pfw.get("applicationNumberText") or "Application # not found"
    )
    #print("Filling patent_info")
    patent_info = {
        "patent_number": patent_number,
        "title": meta.get("inventionTitle") or "(No Title)",
        "filing_date": meta.get("filingDate") or meta.get("effectiveFilingDate"),
        "grant_date": meta.get("grantDate") or meta.get("pctPublicationDate"),
        "pta_days": pfw.get("patentTermAdjustmentData", {}).get("adjustmentTotalQuantity"),
        "status": meta.get("applicationStatusDescriptionText"),
        "application_number": application_number,
        "publication_number": meta.get("earliestPublicationNumber") or meta.get("pctPublicationNumber"),
        "publication_date": meta.get("earliestPublicationDate") or meta.get("pctPublicationDate"),
        "inventors": [
            inv.get("inventorNameText")
            for inv in meta.get("inventorBag", [])
        ],
        "assignees": [
            {
                "name": a.get("assigneeNameText"),
                "pdf_url": assign.get("assignmentDocumentLocationURI")
            }
            for assign in pfw.get("assignmentBag", [])
            for a in assign.get("assigneeBag", [])
        ],
        "parent_continuity": [
            {
                "parent_number": p.get("parentApplicationNumberText"),
                "parent_patent_number": p.get("parentPatentNumber"),
                "filing_date": p.get("parentApplicationFilingDate"),
                "status": p.get("parentApplicationStatusDescriptionText"),
                "child_number": p.get("childApplicationNumberText"),
            }
            for p in pfw.get("parentContinuityBag", [])
        ],
        "child_continuity": [
            {
                "parent_number": c.get("parentApplicationNumberText"),
                "child_patent_number": c.get("childPatentNumber"),
                "child_number": c.get("childApplicationNumberText"),
                "filing_date": c.get("childApplicationFilingDate"),
                "status": c.get("childApplicationStatusDescriptionText"),
            }
            for c in pfw.get("childContinuityBag", [])
        ],
    }

    events = pfw.get("eventDataBag", [])

    # Try to fetch PTAB proceeding list if there's a valid patent or application number
    proceedings = []
    id_to_try = None
    if patent_number not in [None, "", "Patent # not found"]:
        id_to_try = patent_number
    elif application_number not in [None, "", "Application # not found"]:
        id_to_try = application_number
    if id_to_try:
        proceedings = search_ptab_by_id(id_to_try)

    return patent_info, events, proceedings

# Incrementally request all search hits based on passed query q
# Returns the hits in pfws and total = count of the hits, 0 if none
def fetch_all_pages(q, fields=None, limit=1000):
    #Max page_size in current USPTO API is 100
    page_size = 100
    offset = 0
    all_pfws = []
    headers = {
        "accept": "application/json",
        "X-API-KEY": API_KEY,
        "Content-Type": "application/json",
    }
    max_retries = 4

    while True:
        # Adjust page_size to not exceed remaining needed if limit is set
        actual_limit = limit - len(all_pfws) if limit is not None else page_size
        current_page_size = min(actual_limit, page_size)

        payload = {
            "q": q,
            "pagination": {"offset": offset, "limit": current_page_size}
        }
        if fields:
            payload["fields"] = fields

        delay = 1
        for attempt in range(max_retries):
            try:
                resp = requests.post(SEARCH_URL, json=payload, headers=headers, timeout=(5, 30))
                #print(f"✅ Got response: status={resp.status_code}")

                if resp.status_code == 429:
                    print(f"⚠️ Rate limited, sleeping {delay}s")
                    time.sleep(delay)
                    delay *= 2
                    continue

                if resp.status_code == 404:
                    print(f"❌ 404 Not Found for query: {q}")
                    return 0, []

                resp.raise_for_status()
                break
            except Exception as e:
                print(f"❌ Attempt {attempt+1} failed: {e}")
                if attempt == max_retries - 1:
                    raise RateLimitExceeded(f"Rate limit exceeded or error on attempt {attempt + 1}: {e}")
                time.sleep(delay)
                delay *= 2

        try:
            data = resp.json()
        except json.JSONDecodeError as e:
            print(f"❌ JSON decode error: {e}")
            raise

        if "patentFileWrapperDataBag" not in data:
            raise ValueError(f"Missing key 'patentFileWrapperDataBag'. Response: {data}")

        pfws = data.get("patentFileWrapperDataBag", [])
        all_pfws.extend(pfws)
        total = data.get("count", 0)

        offset += current_page_size

        if len(all_pfws) >= total:
            print(f"✅ Reached end of available results: {len(all_pfws)} of {total}")
            break

        if limit is not None and len(all_pfws) >= limit:
            print(f"✅ Reached user-defined limit: {limit} (available: {total})")
            break

    return  (total, all_pfws)

#Retreives list of PTAB documents for a given proceeding and populates them in returned docs
def get_ptab_documents(proceeding_number):
    """
    #TODO: handle records in excess of 500
    Retrieve documents for a given PTAB proceeding number.
    """
    url = f"https://developer.uspto.gov/ptab-api/documents?proceedingNumber={proceeding_number}&recordTotalQuantity=500"
    try:
        resp = requests.get(url, headers={"accept": "application/json"}, timeout=(5, 30))
        resp.raise_for_status()
        docs = []
        for item in resp.json().get("results", []):
            docs.append({
                "filing_date": item.get("documentFilingDate", "—"),
                "document_type": item.get("documentTypeName", "—"),
                "document_number": item.get("documentNumber", "—"),
                "document_identifier": item.get("documentIdentifier"),
                "document_name": item.get("documentName", "—"),
            })
        return docs
    except Exception as e:
        print(f"Error fetching PTAB documents: {e}")
        return []

# Finds if any PTAB proceedings are associated with the passed reference (pat#, app#,docket#, party name)
# Returns proceedings, including patent #, application # and PO name
def search_ptab_by_id(id, all=False):    
    url_fields = [
        "patentNumber",
        "applicationNumberText",
        "proceedingNumber",
        "patentOwnerName",
        "partyName"
    ]
    proceedings = []
    seen_numbers = set()  # Prevent duplicates if same proceeding appears in multiple fields

    for field in url_fields:
        try:
            url = f"https://developer.uspto.gov/ptab-api/proceedings?{field}={id}&recordTotalQuantity=1000"
            resp = requests.get(url, headers={"accept": "application/json"}, timeout=(5, 30))
            resp.raise_for_status()
            results = resp.json().get("results", [])

            for r in results:
                proc_num = r.get("proceedingNumber")
                if proc_num and proc_num not in seen_numbers:
                    seen_numbers.add(proc_num)
                    proceedings.append({
                        "number": proc_num,
                        "status": r.get("proceedingStatusCategory"),
                        "petitioner": r.get("petitionerPartyName"),
                        "filing_date": r.get("proceedingFilingDate"),
                        "ptab_patent_number": r.get("respondentPatentNumber"),
                        "ptab_application_number": r.get("respondentApplicationNumberText"),
                        "ptab_patent_owner": r.get("respondentPartyName"),
                    })

            if proceedings and not all:
                break  # Exit early if we've found matches and not running in "all" mode

        except Exception as e:
            print(f"PTAB fetch error using field={field}; id={id}: {e}")
            continue

    return proceedings

#Logic to handle download and OCR of patent using headless browswer since
#no PDF API that doesn't require huge TAR download
#=================================
@app.route("/uspto_pdf/<patent_number>")
def uspto_pdf(patent_number):
    cached_path = os.path.join(PDF_CACHE_DIR, f"{patent_number}.pdf")
    raw_path = os.path.join(PDF_CACHE_DIR, f"{patent_number}_raw.pdf")
    log_path = os.path.join(PDF_CACHE_DIR, f"{patent_number}.log")

    if os.path.exists(cached_path):
        return send_file(cached_path, mimetype="application/pdf")

    if os.path.exists(raw_path):
        return render_template("choose_pdf.html", patent_number=patent_number)

    thread = threading.Thread(target=download_raw_pdf, args=(patent_number,), daemon=True)
    thread.start()
    return redirect(url_for("ocr_progress", patent_number=patent_number))

#Gets the PDF from ppubs if requested
def download_raw_pdf(patent_number):
    raw_path = os.path.join(PDF_CACHE_DIR, f"{patent_number}_raw.pdf")
    log_path = os.path.join(PDF_CACHE_DIR, f"{patent_number}.log")

    try:
        with open(log_path, "a") as log:
            log.write("🔍 Starting raw PDF lookup...\n")

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto("https://ppubs.uspto.gov/pubwebapp/static/pages/ppubsbasic.html")
            page.fill("#quickLookupTextInput", str(patent_number))
            page.keyboard.press("Enter")
            page.wait_for_selector("a[href*='downloadPdf']", timeout=10000)
            hrefs = page.locator("a[href*='downloadPdf']").evaluate_all("els => els.map(e => e.href)")
            browser.close()

            pdf_url = next((url for url in hrefs if str(patent_number) in url), None)
            if not pdf_url:
                raise Exception(f"No PDF URL found for {patent_number}")

            print(f"Getting pdf: {pdf_url}")
            resp = requests.get(pdf_url)
            resp.raise_for_status()
            with open(raw_path, "wb") as f:
                f.write(resp.content)

            with open(log_path, "a") as log:
                log.write("📥 Raw PDF downloaded.\n")

    except Exception as e:
        with open(log_path, "a") as log:
            log.write(f"❌ Error downloading raw PDF: {e}\n")
import subprocess

@app.route("/ocr_version")
def ocr_version():
    result = subprocess.run(["ocrmypdf", "--version"], stdout=subprocess.PIPE, text=True)
    return f"OCR version used by app: {result.stdout}"

#OCR with log
def run_ocr(patent_number):
    cached_path = os.path.join(PDF_CACHE_DIR, f"{patent_number}.pdf")
    raw_path = os.path.join(PDF_CACHE_DIR, f"{patent_number}_raw.pdf")
    log_path = os.path.join(PDF_CACHE_DIR, f"{patent_number}.log")

    try:
        if not os.path.exists(raw_path):
            raise Exception("Raw PDF missing; cannot OCR.")

        with open(log_path, "a") as log:
            log.write("🔧 Starting OCR processing...\n")

        ocrmypdf.ocr(raw_path, cached_path, skip_text=True)

        with open(log_path, "a") as log:
            log.write("✅ OCR complete.\n")

    except Exception as e:
        with open(log_path, "a") as log:
            log.write(f"❌ OCR error: {e}\n")

#Handles OCR/raw choice from user
@app.route("/choose_pdf_action/<patent_number>", methods=["POST"])
def choose_pdf_action(patent_number):
    choice = request.form.get("choice")
    if choice == "ocr":
        print(f"🚀 Launching OCR thread for {patent_number}")
        thread = threading.Thread(target=run_ocr, args=(patent_number,), daemon=True)
        thread.start()
        print("Thread started")
        return redirect(url_for("ocr_progress", patent_number=patent_number))
    elif choice == "raw":
        raw_path = os.path.join(PDF_CACHE_DIR, f"{patent_number}_raw.pdf")
        if os.path.exists(raw_path):
            return send_file(raw_path, mimetype="application/pdf")
        else:
            return "Raw PDF not found", 404
    else:
        return "Invalid choice", 400

@app.route("/ocr_progress/<patent_number>")
def ocr_progress(patent_number):
    print(f"Entered OCR_progress for {patent_number}")
    log_path = os.path.join(PDF_CACHE_DIR, f"{patent_number}.log")
    cached_path = os.path.join(PDF_CACHE_DIR, f"{patent_number}.pdf")

    log = ""
    progress = 0

    if os.path.exists(log_path):
        with open(log_path, encoding="utf-8") as f:
            log = f.read()
            progress = extract_progress(log)
    
    raw_downloaded = "📥 Raw PDF downloaded" in log
    ocr_started = "🔧 Starting OCR processing" in log
    ocr_failed  = "❌" in log
    ocr_done    = os.path.exists(cached_path)

    if raw_downloaded and not ocr_started and not ocr_failed and not ocr_done:
        print("⏳ Waiting for OCR to start...")

    return render_template("processing.html",
                           patent_number=patent_number,
                           log=log,
                           progress=progress,
                           ready=ocr_done)

@app.route("/uspto_pdf_download/<patent_number>")
def download_pdf(patent_number):
    cached_path = os.path.join(PDF_CACHE_DIR, f"{patent_number}.pdf")
    if os.path.exists(cached_path):
        return send_file(cached_path, mimetype="application/pdf")
    return "PDF not ready", 404
#==================================

#render PTAB pdf documents if clicked (assuming URL is /download/<doc id>)
@app.route("/download/<document_identifier>")
def download_doc(document_identifier):
    """
    Proxy download of a PTAB document, but tell the browser to display inline.
    """
    dl_url = (
        "https://developer.uspto.gov/ptab-api/documents/"
        f"{document_identifier}/download"
    )
    
    try:
        resp = requests.get(dl_url, headers={"accept": "application/octet-stream"}, timeout=(5, 30))
        resp.raise_for_status()
    except (RequestException, Timeout, HTTPError) as e:
        return f"Error downloading document: {e}", 502

    # 1) Determine filename
    cd = resp.headers.get("Content-Disposition", "")
    _, opts = parse_options_header(cd)
    filename = opts.get("filename", f"{document_identifier}")

    # 2) Determine content-type
    if filename.lower().endswith(".pdf"):
        content_type = "application/pdf"
    else:
        content_type = resp.headers.get("Content-Type", "application/octet-stream")

    # 3) Build Flask response
    flask_resp = Response(resp.content, content_type=content_type)

    # 4) Tell the browser to render inline
    flask_resp.headers["Content-Disposition"] = (
        f'inline; filename="{filename}"'
    )

    return flask_resp

#Return CSV of all results if user clicks from confirm_large_results.html
@app.route("/CSV_download", methods=["POST"])
def csv_download():
    search_term = request.form.get("search_term", "").strip()

    if not search_term:
        return "Missing search term", 400

    try:
        fields = [
            "assignmentBag.assigneeBag.assigneeNameText",
            "applicationNumberText",
            "applicationMetaData.filingDate",
            "applicationMetaData.effectiveFilingDate",
            "applicationMetaData.grantDate",
            "applicationMetaData.pctPublicationDate",
            "applicationMetaData.applicationStatusDescriptionText",
            "applicationMetaData.inventionTitle",
            "applicationMetaData.patentNumber",
            "applicationMetaData.earliestPublicationNumber",
            "applicationMetaData.pctPublicationNumber",
            "applicationMetaData.earliestPublicationDate",
            "patentTermAdjustmentData.adjustmentTotalQuantity",
        ]
        total, pfws = fetch_all_pages(
            search_term,
            fields=fields,
            limit=None
        )

        si = StringIO()
        writer = csv.writer(si)
        writer.writerow([
            "Patent Number",
            "Application Number",
            "Publication Number",
            "Title",
            "Filing Date",
            "Grant Date",
            "PTA Days",
            "Status",
            "Publication Date"
        ])

        base_url = "http://eliotpat.com"

        for pfw in pfws:
            meta = pfw.get("applicationMetaData", {})
            patent_number = meta.get("patentNumber", "")
            application_number = pfw.get("applicationNumberText", "")
            publication_number = meta.get("earliestPublicationNumber") or meta.get("pctPublicationNumber", "")

            # Hyperlink format: =HYPERLINK("http://...","Label")
            patent_link = f'=HYPERLINK("{base_url}?patent_number={patent_number}", "{patent_number}")' if patent_number else ""
            app_link = f'=HYPERLINK("{base_url}?application_number={application_number}", "{application_number}")' if application_number else ""
            pub_link = f'=HYPERLINK("{base_url}?publication_number={publication_number}", "{publication_number}")' if publication_number else ""

            writer.writerow([
                patent_link,
                app_link,
                pub_link,
                meta.get("inventionTitle", "(No Title)"),
                meta.get("filingDate") or meta.get("effectiveFilingDate"),
                meta.get("grantDate") or meta.get("pctPublicationDate"),
                pfw.get("patentTermAdjustmentData", {}).get("adjustmentTotalQuantity"),
                meta.get("applicationStatusDescriptionText", ""),
                meta.get("earliestPublicationDate") or meta.get("pctPublicationDate")
            ])

        return Response(
            si.getvalue(),
            mimetype="text/csv",
            headers={"Content-Disposition": "attachment;filename=bulk_search.csv"}
        )

    except RateLimitExceeded:
        return "USPTO API rate limit reached. Please try again later.", 429
    except Exception as e:
        return f"Unexpected error: {e}", 500

# MISC Helper Functions

# ✅ PATCH: Replace gather_family_tree to use new continuity endpoint with improved error handling and bag caching
def gather_family_tree(start_app_number, seen=None, depth=0, max_depth=40):
    """
    Recursively walks continuity tree using the dedicated USPTO continuity API:
    GET /patent/applications/[application_number]/continuity
    Returns a dict: {app_number: {"bag": ..., "parents": [...], "children": [...]}}
    """
    if seen is None:
        seen = {}

    if depth > max_depth:
        raise RecursionError(f"🔁 Max depth {max_depth} exceeded while traversing from {start_app_number}")

    if start_app_number in seen:
        return seen

    url = f"https://api.uspto.gov/api/v1/patent/applications/{start_app_number}/continuity"
    headers = {
        "accept": "application/json",
        "X-API-KEY": API_KEY,
    }

    max_retries = 4
    delay = 1

    for attempt in range(max_retries):
        try:
            #print(f"Checking: {url}")
            resp = requests.get(url, headers=headers, timeout=(5, 30))
            resp.raise_for_status()
            break
        except requests.HTTPError as e:
            if resp.status_code == 429:
                print(f"⚠️ Rate limited on {start_app_number}, sleeping {delay}s")
                time.sleep(delay)
                delay *= 2
                continue
            elif resp.status_code == 404:
                print(f"⚠️ Application {start_app_number} not found, skipping.")
                seen[start_app_number] = {"bag": {}, "parents": [], "children": []}
                return seen
            raise
        except Exception as e:
            print(f"⚠️ Error in gather_family_tree({start_app_number}): {e}")
            seen[start_app_number] = {"bag": {}, "parents": [], "children": []}
            return seen
    else:
        raise Exception(f"❌ Failed to fetch continuity for {start_app_number} after {max_retries} attempts")

    try:
        data = resp.json()
    except Exception as e:
        print(f"⚠️ JSON decode error: {e}")
        seen[start_app_number] = {"bag": {}, "parents": [], "children": []}
        return seen

    bags = data.get("patentFileWrapperDataBag", [])
    if not bags:
        seen[start_app_number] = {"bag": {}, "parents": [], "children": []}
        return seen

    bag = bags[0]
    parents = bag.get("parentContinuityBag", [])
    children = bag.get("childContinuityBag", [])

    seen[start_app_number] = {
        "bag": bag,
        "parents": parents,
        "children": children,
    }

    for rel in parents:
        app_no = rel.get("parentApplicationNumberText")
        if app_no and app_no not in seen:
            gather_family_tree(app_no, seen, depth + 1, max_depth)

    for rel in children:
        app_no = rel.get("childApplicationNumberText")
        if app_no and app_no not in seen:
            gather_family_tree(app_no, seen, depth + 1, max_depth)

    if depth == 0:
        print(f"✅ Finished family tree for {start_app_number}, total apps collected: {len(seen)}")

    return seen

def sort_family_members(members):
    def sort_key(member):
        app_num = member.get("application_number", "")
        if app_num.startswith("PCT") or app_num.startswith("WO"):
            return (1, float("inf"))  # Put PCT/WO at the bottom
        try:
            numeric_part = "".join(filter(str.isdigit, app_num))
            return (0, int(numeric_part))
        except Exception:
            return (0, float("inf"))
    return sorted(members, key=sort_key)

def extract_progress(log_text):
    # Look for last occurrence of "NN%" (e.g., 65%) in the log
    matches = re.findall(r'(\d{1,3})%', log_text)
    if matches:
        return int(matches[-1])
    return 0

def parse_doc_number(s):
            try:
                return int(s)
            except Exception:
                return -1

def parse_date(s):
    try:
        return parser.parse(s)
    except Exception:
        return datetime.max

class RateLimitExceeded(Exception):
    pass


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
