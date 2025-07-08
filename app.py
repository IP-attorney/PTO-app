# app.py
import os, requests
import re
import time
import tarfile
import tempfile
import os
from flask import send_file
from datetime import datetime
from flask import Flask, render_template, request, Response
from werkzeug.http import parse_options_header

app = Flask(__name__)
PDF_CACHE_DIR = "uspto_pdf_cache"
os.makedirs(PDF_CACHE_DIR, exist_ok=True)
API_KEY     = os.getenv("USPTO_API_KEY", "krvrixyvojizidrrudrbjzwxcbdsnr")
SEARCH_URL  = "https://api.uspto.gov/api/v1/patent/applications/search"

class RateLimitExceeded(Exception):
    pass

def fetch_all_pages(q, fields=None):
    """
    Run a USPTO search for query `q`.  If `fields` is given, include that restriction.
    Paginate through all results in blocks of 100 and return (total_count, list_of_pfws).
    Retries up to 5 times if we hit a 429.
    """
    limit      = 100
    offset     = 0
    all_pfws   = []
    headers    = {
        "accept":       "application/json",
        "X-API-KEY":    API_KEY,
        "Content-Type": "application/json",
    }
    max_retries = 5

    while True:
        payload = {"q": q, "pagination": {"offset": offset, "limit": limit}}
        if fields:
            payload["fields"] = fields

        # --- retry loop on 429 ---
        delay = 1
        for attempt in range(max_retries):
            resp = requests.post(SEARCH_URL, json=payload, headers=headers)
            if resp.status_code == 429:
                time.sleep(delay)
                delay *= 2
                continue
            # for any other status, either OK or error
            resp.raise_for_status()
            break
        else:
            # exhausted retries
            raise RateLimitExceeded(f"Rate limit exceeded after {max_retries} attempts")

        data = resp.json()
        pfws = data.get("patentFileWrapperDataBag", [])
        all_pfws.extend(pfws)

        total = data.get("count", 0)
        if len(all_pfws) >= total:
            return total, all_pfws

        offset += limit





@app.route("/uspto_pdf/<patent_number>")
def uspto_pdf(patent_number):
    try:
        # 1. Check if already cached
        cached_path = os.path.join(PDF_CACHE_DIR, f"{patent_number}.pdf")
        if os.path.exists(cached_path):
            return send_file(cached_path, mimetype="application/pdf")

        # 2. Lookup grant date
        q = f"applicationMetaData.patentNumber:{patent_number}"
        total, pfws = fetch_all_pages(q)
        if total == 0:
            return f"Patent {patent_number} not found", 404

        grant_date_str = pfws[0]["applicationMetaData"].get("grantDate")
        if not grant_date_str:
            return f"No grant date found for {patent_number}", 404

        grant_date = datetime.strptime(grant_date_str, "%Y-%m-%d")
        date_str = grant_date.strftime("%Y-%m-%d")
        yyyymmdd = grant_date.strftime("%Y%m%d")

        # 3. Query bulk API
        bulk_url = "https://api.uspto.gov/api/v1/datasets/products/ptgrmp2"
        params = {
            "fileDataFromDate": date_str,
            "fileDataToDate": date_str,
            "includeFiles": "true"
        }
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "x-api-key": API_KEY
        }

        r = requests.get(bulk_url, headers=headers, params=params)
        r.raise_for_status()
        data = r.json()
        file_bag = (
            data.get("bulkDataProductBag", [{}])[0]
            .get("productFileBag", {})
            .get("fileDataBag", [])
        )
        if not file_bag:
            return f"No .tar file found for grant date {date_str}", 404

        file_uri = file_bag[0]["fileDownloadURI"]

        # 4. Download .tar file to temp
        with requests.get(file_uri, headers=headers, stream=True) as resp:
            resp.raise_for_status()
            with tempfile.NamedTemporaryFile(delete=False, suffix=".tar") as tmp_tar:
                for chunk in resp.iter_content(chunk_size=8192):
                    tmp_tar.write(chunk)
                tmp_tar_path = tmp_tar.name

        # 5. Extract just the needed PDF
        part1 = patent_number[0:2]
        part2 = patent_number[2:5]
        part3 = patent_number[5:]
        internal_path = f"P{yyyymmdd}-{yyyymmdd}/{part1}/{part2}/{part3}/{patent_number}.pdf"

        with tarfile.open(tmp_tar_path, "r") as tar:
            try:
                member = tar.getmember(internal_path)
            except KeyError:
                return f"Patent PDF not found in archive: {internal_path}", 404
            extracted = tar.extractfile(member)
            if not extracted:
                return f"Could not extract {internal_path}", 500

            # 6. Cache to disk
            with open(cached_path, "wb") as f:
                f.write(extracted.read())

        # 7. Serve the cached PDF
        return send_file(cached_path, mimetype="application/pdf")

    except Exception as e:
        return f"Error fetching USPTO PDF for {patent_number}: {e}", 500


@app.route("/download/<document_identifier>")
def download_doc(document_identifier):
    """
    Proxy download of a PTAB document, but tell the browser to display inline.
    """
    dl_url = (
        "https://developer.uspto.gov/ptab-api/documents/"
        f"{document_identifier}/download"
    )
    resp = requests.get(dl_url, headers={"accept": "application/octet-stream"})
    resp.raise_for_status()

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



@app.route("/", methods=["GET", "POST"])
def home():
    search_term = ""
    proceeding_number = ""
    documents = []
    results = None
    error = None
    patent_info = None
    events = []
    proceedings = []

    publication_number  = request.args.get("publication_number", "").strip()
    patent_number       = request.args.get("patent_number", "").strip()
    application_number  = request.args.get("application_number", "").strip()

    # Handle form POST
    if request.method == "POST":
        search_term = request.form.get("search_term", "").strip()
        if re.match(r"^[A-Za-z]+\d{4}-\d+$", search_term):
            proceeding_number = search_term

    # Handle GET: PTAB document view
    elif request.method == "GET":
        proceeding_number = request.args.get("proceeding_number", "").strip()
        search_term = request.args.get("search_term", "").strip()

    # ─── PTAB DOCUMENTS ───
    if proceeding_number:
        try:
            limit = 500
            total = None
            offset = 1
            while True:
                params = {
                    "proceedingNumber": proceeding_number,
                    "recordTotalQuantity": limit
                }
                if total is not None:
                    params["recordStartNumber"] = offset

                resp = requests.get(
                    "https://developer.uspto.gov/ptab-api/documents",
                    params=params,
                    headers={"accept": "application/json"}
                )
                resp.raise_for_status()
                data = resp.json()

                if total is None:
                    total = data.get("recordTotalQuantity", 0)

                for entry in data.get("results", []):
                    documents.append({
                        "filing_date":         entry.get("documentFilingDate"),
                        "document_type":       entry.get("documentTypeName"),
                        "document_number":     entry.get("documentNumber"),
                        "document_name":       entry.get("documentName"),
                        "document_identifier": entry.get("documentIdentifier"),
                    })

                if len(documents) >= total:
                    break

                offset = len(documents) + 1

            # Sort documents by filing_date, then document_type, then document_number
            from datetime import datetime
            documents.sort(key=lambda d: (
                datetime.strptime(d["filing_date"], "%m-%d-%Y"),
                d.get("document_type", ""),
                int(d.get("document_number", 0))
            ))

        except Exception as e:
            documents = []
            error = f"Error fetching documents for proceeding {proceeding_number}: {e}"

        return render_template("index.html", documents=documents, error=error)

    # ─── DETAIL LOOKUP ───
    if publication_number or patent_number or application_number:
        if publication_number:
            q = publication_number
        elif patent_number:
            q = f"applicationMetaData.patentNumber:{patent_number}"
        else:
            q = f"applicationNumberText:{application_number}"

        total, pfws = fetch_all_pages(q)
        if total == 0:
            error = f"No results found for '{publication_number or patent_number or application_number}'."
        else:
            pfw = pfws[0]
            meta = pfw.get("applicationMetaData", {})

            patent_info = {
                "patent_number":      meta.get("patentNumber"),
                "title":              meta.get("inventionTitle"),
                "filing_date":        meta.get("filingDate"),
                "grant_date":         meta.get("grantDate"),
                "pta_days":           pfw.get("patentTermAdjustmentData", {}).get("adjustmentTotalQuantity"),
                "status":             meta.get("applicationStatusDescriptionText"),
                "application_number": pfw.get("applicationNumberText"),
                "publication_number": meta.get("earliestPublicationNumber"),
                "publication_date":   meta.get("earliestPublicationDate"),
                "inventors": [
                    inv.get("inventorNameText")
                    for inv in meta.get("inventorBag", [])
                ],
                "assignees": [
                    {
                        "name":    a.get("assigneeNameText"),
                        "pdf_url": assign.get("assignmentDocumentLocationURI")
                    }
                    for assign in pfw.get("assignmentBag", [])
                    for a in assign.get("assigneeBag", [])
                ],
                "parent_continuity": [
                    {
                        "parent_number":        p.get("parentApplicationNumberText"),
                        "parent_patent_number": p.get("parentPatentNumber"),
                        "filing_date":          p.get("parentApplicationFilingDate"),
                        "status":               p.get("parentApplicationStatusDescriptionText"),
                        "child_number":         p.get("childApplicationNumberText"),
                    }
                    for p in pfw.get("parentContinuityBag", [])
                ],
                "child_continuity": [
                    {
                        "parent_number":       c.get("parentApplicationNumberText"),
                        "child_patent_number": c.get("childPatentNumber"),
                        "child_number":        c.get("childApplicationNumberText"),
                        "filing_date":         c.get("childApplicationFilingDate"),
                        "status":              c.get("childApplicationStatusDescriptionText"),
                    }
                    for c in pfw.get("childContinuityBag", [])
                ],
            }
            events = pfw.get("eventDataBag", [])

            # fetch PTAB proceedings
            try:
                pnum = patent_info["patent_number"]
                url = f"https://developer.uspto.gov/ptab-api/proceedings?patentNumber={pnum}&recordTotalQuantity=2000"
                resp = requests.get(url, headers={"accept": "application/json"})
                resp.raise_for_status()
                for e in resp.json().get("results", []):
                    proceedings.append({
                        "number":     e.get("proceedingNumber"),
                        "status":     e.get("proceedingStatusCategory"),
                        "petitioner": e.get("petitionerPartyName"),
                    })
            except Exception:
                pass

    # ─── GENERIC SEARCH ───
    elif search_term:
        fields = [
            "assignmentBag.assigneeBag.assigneeNameText",
            "applicationNumberText",
            "applicationMetaData.filingDate",
            "applicationMetaData.applicationStatusDescriptionText",
            "applicationMetaData.inventionTitle",
            "applicationMetaData.patentNumber"
        ]
        total, pfws = fetch_all_pages(search_term, fields=fields)

        if total == 0:
            error = f"No results found for '{search_term}'."
        elif total == 1:
            _, full_pfws = fetch_all_pages(search_term)
            pfw = full_pfws[0]
            meta = pfw.get("applicationMetaData", {})

            patent_info = {
                "patent_number":      meta.get("patentNumber"),
                "title":              meta.get("inventionTitle"),
                "filing_date":        meta.get("filingDate"),
                "grant_date":         meta.get("grantDate"),
                "pta_days":           pfw.get("patentTermAdjustmentData", {}).get("adjustmentTotalQuantity"),
                "status":             meta.get("applicationStatusDescriptionText"),
                "application_number": pfw.get("applicationNumberText"),
                "publication_number": meta.get("earliestPublicationNumber"),
                "publication_date":   meta.get("earliestPublicationDate"),
                "inventors": [
                    inv.get("inventorNameText")
                    for inv in meta.get("inventorBag", [])
                ],
                "assignees": [
                    {
                        "name":    a.get("assigneeNameText"),
                        "pdf_url": assign.get("assignmentDocumentLocationURI")
                    }
                    for assign in pfw.get("assignmentBag", [])
                    for a in assign.get("assigneeBag", [])
                ],
                "parent_continuity": [
                    {
                        "parent_number":        p.get("parentApplicationNumberText"),
                        "parent_patent_number": p.get("parentPatentNumber"),
                        "filing_date":          p.get("parentApplicationFilingDate"),
                        "status":               p.get("parentApplicationStatusDescriptionText"),
                        "child_number":         p.get("childApplicationNumberText"),
                    }
                    for p in pfw.get("parentContinuityBag", [])
                ],
                "child_continuity": [
                    {
                        "parent_number":       c.get("parentApplicationNumberText"),
                        "child_patent_number": c.get("childPatentNumber"),
                        "child_number":        c.get("childApplicationNumberText"),
                        "filing_date":         c.get("childApplicationFilingDate"),
                        "status":              c.get("childApplicationStatusDescriptionText"),
                    }
                    for c in pfw.get("childContinuityBag", [])
                ],
            }
            events = pfw.get("eventDataBag", [])

            try:
                pnum = patent_info["patent_number"]
                url = f"https://developer.uspto.gov/ptab-api/proceedings?patentNumber={pnum}&recordTotalQuantity=2000"
                resp = requests.get(url, headers={"accept": "application/json"})
                resp.raise_for_status()
                for e in resp.json().get("results", []):
                    proceedings.append({
                        "number":     e.get("proceedingNumber"),
                        "status":     e.get("proceedingStatusCategory"),
                        "petitioner": e.get("petitionerPartyName"),
                    })
            except Exception:
                pass

        else:
            results = []
            for pfw in pfws:
                meta = pfw.get("applicationMetaData", {})
                assignees = ", ".join(
                    a.get("assigneeNameText")
                    for assign in pfw.get("assignmentBag", [])
                    for a in assign.get("assigneeBag", [])
                    if a.get("assigneeNameText")
                )
                results.append({
                    "application_number": pfw.get("applicationNumberText"),
                    "patent_number":      meta.get("patentNumber"),
                    "filing_date":        meta.get("filingDate"),
                    "status":             meta.get("applicationStatusDescriptionText"),
                    "title":              meta.get("inventionTitle"),
                    "assignees":          assignees
                })

    return render_template(
        "index.html",
        search_term=search_term,
        patent=patent_info,
        events=events,
        results=results,
        proceedings=proceedings,
        error=error
    )






if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
