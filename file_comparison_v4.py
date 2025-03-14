"""
Remote Folder Comparison Tool with Flask Web Interface.

Features:
  • Recursively lists files on two remote servers via SSH/SFTP.
  • Compares file content (line-by-line diff for text files, streaming MD5 for large/binary files).
  • Supports DOCX and Excel text extraction (if libraries installed).
  • Exception filtering based on:
      - Folders,
      - File extensions,
      - Exact file names,
      - File name substrings.
  • Date threshold filtering (only files modified on/after a given date).
  • Parallel processing of file comparisons using ThreadPoolExecutor.
  • HTML reports with summary statistics, interactive filters, and a link to view the log.
  • A separate Exception Report that includes a timestamp and a table of all excluded files
    (with modification times from both servers).
  • Configuration via JSON (use null for “None”) or via an input form.
  • An interactive Flask web interface:
      - When a JSON file is uploaded, the individual form fields are hidden and their validation is skipped.
      - When the submit button is clicked, the form is visually “disabled” (via CSS) and a loading message is shown.
  • Dynamic file serving via a /download route (compatible with Linux and Windows).

Usage (CLI mode):
  python compare_remote_folders.py <serverA> <userA> <keyA_or_None> <passA_or_None> <remote_folderA>
                                  <serverB> <userB> <keyB_or_None> <passB_or_None> <remote_folderB>
                                  [compare_list_file_or_None] [output_folder_or_None] [follow_symlinks (True/False)]
                                  [exception_folders_file_or_None] [exception_extensions_file_or_None] [date_threshold_or_None]
                                  [file_exceptions_file_or_None] [file_substring_exceptions_file_or_None]
                                  [config_file_or_None]

Usage (Web mode):
  python compare_remote_folders.py --web
  (Then open http://0.0.0.0:5000 in your browser.)

Example JSON config (use null instead of "None"):
{
  "serverA": "192.168.1.10",
  "userA": "alice",
  "keyA": null,
  "passA": null,
  "remote_folderA": "/var/www/html",
  "serverB": "203.0.113.5",
  "userB": "bob",
  "keyB": null,
  "passB": "MyPassword",
  "remote_folderB": "/var/www/html",
  "compare_list_file": "compare_list.txt",
  "output_folder": "MyOutput",
  "follow_symlinks": true,
  "exception_folders": "exception_folders.txt",
  "exception_extensions": "exception_extensions.txt",
  "date_threshold": "2023-01-01 00:00:00",
  "file_exceptions": "file_exceptions.txt",
  "file_substring_exceptions": "file_substring_exceptions.txt",
  "max_workers": 10
}
"""

import os
import sys
import stat
import difflib
import logging
import posixpath
import hashlib
import io
import json
import getpass
import threading
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
import base64

import paramiko
from paramiko.ssh_exception import AuthenticationException

# Optional libraries for file format extraction.
try:
    from docx import Document
except ImportError:
    Document = None
    print("Warning: python-docx is not installed. DOCX files will be treated as binary.")
try:
    import openpyxl
except ImportError:
    openpyxl = None
    print("Warning: openpyxl is not installed. Excel files will be treated as binary.")

# Optional library for configuration validation using JSON Schema.
try:
    import jsonschema
except ImportError:
    jsonschema = None

# For Flask web interface.
try:
    from flask import Flask, request, render_template_string, send_from_directory
except ImportError:
    Flask = None

# Set a threshold for large files (10 MB).
LARGE_FILE_THRESHOLD = 10 * 1024 * 1024

# Setup logger for the application.
logger = logging.getLogger("remote_diff")
logger.setLevel(logging.DEBUG)

# Use thread-local storage for SFTP connections; each thread gets its own SFTP client.
thread_local = threading.local()

# Global list and lock for recording excluded files (for the exception report).
exclusion_records = []
exclusion_lock = threading.Lock()

# --------------------------------------------------
# Configuration Validation
# --------------------------------------------------
def validate_config(config):
    """
    Validates the JSON configuration using a schema.
    Uses jsonschema if available; otherwise, performs minimal manual checks.
    """
    schema = {
        "type": "object",
        "properties": {
            "serverA": {"type": "string"},
            "userA": {"type": "string"},
            "remote_folderA": {"type": "string"},
            "serverB": {"type": "string"},
            "userB": {"type": "string"},
            "remote_folderB": {"type": "string"},
            "compare_list_file": {"type": ["string", "null"]},
            "output_folder": {"type": "string"},
            "follow_symlinks": {"type": "boolean"},
            "exception_folders": {"type": ["string", "array"]},
            "exception_extensions": {"type": ["string", "array"]},
            "date_threshold": {"type": ["string", "null"]},
            "file_exceptions": {"type": ["string", "array"]},
            "file_substring_exceptions": {"type": ["string", "array"]},
            "max_workers": {"type": "integer"}
        },
        "required": ["serverA", "userA", "remote_folderA", "serverB", "userB", "remote_folderB"]
    }
    if jsonschema is not None:
        try:
            jsonschema.validate(instance=config, schema=schema)
        except jsonschema.ValidationError as e:
            raise ValueError(f"Configuration validation error: {e.message}")
    else:
        # Minimal validation if jsonschema is not available.
        required_keys = ["serverA", "userA", "remote_folderA", "serverB", "userB", "remote_folderB"]
        missing = [k for k in required_keys if k not in config or not config[k]]
        if missing:
            raise ValueError(f"Missing required configuration keys: {', '.join(missing)}")
    return config

# --------------------------------------------------
# Logging Setup Function
# --------------------------------------------------
def setup_logging(output_folder):
    """
    Clears existing log handlers and sets up new ones for process and debug logs,
    as well as a console handler.
    """
    logger.handlers.clear()
    process_log_path = os.path.join(output_folder, "process.log")
    debug_log_path = os.path.join(output_folder, "debug.log")
    formatter_obj = logging.Formatter('%(asctime)s %(levelname)s %(message)s')

    process_handler = logging.FileHandler(process_log_path, encoding='utf-8')
    process_handler.setLevel(logging.INFO)
    process_handler.setFormatter(formatter_obj)
    logger.addHandler(process_handler)

    debug_handler = logging.FileHandler(debug_log_path, encoding='utf-8')
    debug_handler.setLevel(logging.DEBUG)
    debug_handler.setFormatter(formatter_obj)
    logger.addHandler(debug_handler)

    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(formatter_obj)
    logger.addHandler(console_handler)

# --------------------------------------------------
# SFTP Client Management (Thread-Local)
# --------------------------------------------------
def get_sftp_client_for(server, user, key, password, attr_name):
    """
    Retrieves (or creates) a thread-local SFTP client for the given server credentials.
    """
    if not hasattr(thread_local, attr_name):
        ssh = connect_ssh(server, user, key, password)
        setattr(thread_local, attr_name, ssh.open_sftp())
    return getattr(thread_local, attr_name)

# --------------------------------------------------
# Exception List Loader
# --------------------------------------------------
def load_exception_list(value):
    """
    Loads exception lists either from a file (if the string is a path),
    from a list if already a list, or splits a string into lines.
    """
    if isinstance(value, str) and os.path.exists(value):
        with open(value, "r", encoding="utf-8") as f:
            return [line.strip() for line in f if line.strip()]
    elif isinstance(value, list):
        return value
    elif isinstance(value, str) and value.strip() != "":
        return [line.strip() for line in value.splitlines() if line.strip()]
    return []

# --------------------------------------------------
# Record Exclusions for Files That Are Not Compared
# --------------------------------------------------
def record_exclusion(rel_path, reason, file_a_tuple=None, file_b_tuple=None, sftpA=None, sftpB=None):
    """
    Records an excluded file along with its reason and modification times from both servers.
    """
    mtimeA = "N/A"
    mtimeB = "N/A"
    if sftpA and file_a_tuple:
        try:
            mtimeA = datetime.fromtimestamp(sftpA.stat(file_a_tuple[1]).st_mtime).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            pass
    if sftpB and file_b_tuple:
        try:
            mtimeB = datetime.fromtimestamp(sftpB.stat(file_b_tuple[1]).st_mtime).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            pass
    with exclusion_lock:
        exclusion_records.append({
            "file": rel_path,
            "reason": reason,
            "mtime_A": mtimeA,
            "mtime_B": mtimeB
        })

# --------------------------------------------------
# Interactive Handler for Keyboard-Interactive SSH Authentication
# --------------------------------------------------
def interactive_handler(title, instructions, prompt_list):
    """
    Handles keyboard-interactive authentication by prompting the user for input.
    """
    responses = []
    print(title)
    print(instructions)
    for prompt, show_input in prompt_list:
        if show_input:
            responses.append(getpass.getpass(prompt))
        else:
            responses.append(input(prompt))
    return responses

# --------------------------------------------------
# SSH Connection Setup
# --------------------------------------------------
def connect_ssh(server, username, key, password):
    """
    Establishes an SSH connection using key-based or password-based authentication.
    Falls back to keyboard-interactive if necessary.
    """
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    if key in ["None", ""]:
        key = None
    if password in ["None", ""]:
        password = None
    try:
        if key and password:
            ssh.connect(server, username=username, key_filename=key, passphrase=password)
        elif key:
            ssh.connect(server, username=username, key_filename=key)
        elif password:
            ssh.connect(server, username=username, password=password)
        else:
            ssh.connect(server, username=username)
    except AuthenticationException:
        logger.warning(f"Standard authentication failed for {server}. Trying keyboard-interactive.")
        try:
            transport = paramiko.Transport((server, 22))
            transport.start_client()
            transport.auth_interactive(username, interactive_handler)
            ssh._transport = transport
        except Exception as ie:
            logger.error(f"Keyboard-interactive authentication failed for {server}: {ie}")
            raise
    except Exception as e:
        logger.error(f"Error connecting to {server} as {username}: {e}")
        raise
    return ssh

# --------------------------------------------------
# Recursive Remote File Listing via SFTP
# --------------------------------------------------
def list_remote_files(sftp, remote_dir, follow_symlinks=False):
    """
    Recursively lists all files in the specified remote directory via SFTP.
    Follows symlinks if specified (while detecting cycles).
    """
    file_dict = {}
    visited = set()
    try:
        items = sftp.listdir_attr(remote_dir)
        logger.info(f"Successfully listed: {remote_dir}")
        logger.debug(f"Directory listing for {remote_dir}: {[item.filename for item in items]}")
    except Exception as e:
        logger.error(f"Error listing directory {remote_dir}: {e}")
        return file_dict

    def recursive_list(current_path, base):
        try:
            for attr in sftp.listdir_attr(current_path):
                full_path = posixpath.join(current_path, attr.filename)
                logger.debug(f"Found: {full_path}")
                if stat.S_ISLNK(attr.st_mode):
                    # Handle symlink
                    try:
                        link_target = sftp.readlink(full_path)
                        logger.debug(f"Symlink: {full_path} -> {link_target}")
                        if follow_symlinks:
                            if not link_target.startswith('/'):
                                link_target = posixpath.join(posixpath.dirname(full_path), link_target)
                            if link_target in visited:
                                logger.warning(f"Cycle detected for symlink {full_path}, skipping follow.")
                                continue
                            visited.add(link_target)
                            try:
                                target_attr = sftp.stat(link_target)
                                if stat.S_ISDIR(target_attr.st_mode):
                                    recursive_list(link_target, base)
                                else:
                                    rel_path = posixpath.relpath(full_path, base)
                                    file_dict[rel_path] = ("file", link_target)
                            except Exception as e:
                                logger.error(f"Error stating symlink target {link_target}: {e}")
                                rel_path = posixpath.relpath(full_path, base)
                                file_dict[rel_path] = ("symlink", full_path, link_target)
                        else:
                            rel_path = posixpath.relpath(full_path, base)
                            file_dict[rel_path] = ("symlink", full_path, link_target)
                    except Exception as e:
                        logger.error(f"Error reading symlink {full_path}: {e}")
                    continue
                elif stat.S_ISDIR(attr.st_mode):
                    # Recurse into subdirectory
                    recursive_list(full_path, base)
                else:
                    # Regular file; record its relative path and full path.
                    rel_path = posixpath.relpath(full_path, base)
                    file_dict[rel_path] = ("file", full_path)
        except Exception as e:
            logger.error(f"Error accessing {current_path}: {e}")

    recursive_list(remote_dir, remote_dir)
    return file_dict

# --------------------------------------------------
# File Format Handlers (Modular & Extensible)
# --------------------------------------------------
def handle_docx(data, full_path):
    """
    Extracts text from a DOCX file using python-docx.
    Returns a tuple (is_binary, content) where is_binary is False if text is extracted.
    """
    if Document is None:
        logger.warning(f"python-docx not available. Treating {full_path} as binary.")
        return (True, data)
    try:
        bio = io.BytesIO(data)
        doc = Document(bio)
        text = "\n".join(p.text for p in doc.paragraphs)
        logger.debug(f"Extracted text from DOCX file {full_path}.")
        return (False, text.splitlines())
    except Exception as e:
        logger.error(f"Error processing DOCX file {full_path}: {e}")
        return (True, data)

def handle_excel(data, full_path):
    """
    Extracts text from an Excel file using openpyxl.
    Returns a tuple (is_binary, content) where is_binary is False if text is extracted.
    """
    if openpyxl is None:
        logger.warning(f"openpyxl not available. Treating {full_path} as binary.")
        return (True, data)
    try:
        bio = io.BytesIO(data)
        wb = openpyxl.load_workbook(bio, read_only=True, data_only=True)
        texts = []
        for sheet in wb.worksheets:
            texts.append(f"Sheet: {sheet.title}")
            for row in sheet.iter_rows(values_only=True):
                row_text = "\t".join(str(cell) if cell is not None else "" for cell in row)
                texts.append(row_text)
        text = "\n".join(texts)
        logger.debug(f"Extracted text from Excel file {full_path}.")
        return (False, text.splitlines())
    except Exception as e:
        logger.error(f"Error processing Excel file {full_path}: {e}")
        return (True, data)

def handle_pdf(data, full_path):
    """
    Placeholder for PDF extraction.
    Currently, it logs that PDF extraction is not implemented and treats the file as binary.
    """
    logger.info(f"PDF extraction for {full_path} is not yet implemented. Treating as binary.")
    return (True, data)

# Mapping of file extensions to their respective handler functions.
FILE_FORMAT_HANDLERS = {
    ".docx": handle_docx,
    ".xlsx": handle_excel,
    ".xlsm": handle_excel,
    # ".pdf": handle_pdf,  # Uncomment if PDF extraction is implemented
}

# --------------------------------------------------
# Get File Content with Extended Format Support
# --------------------------------------------------
def get_file_content(sftp, remote_path_info):
    """
    Reads the file content via SFTP. Uses streaming MD5 for large files,
    and applies the appropriate file format handler if available.
    Returns a tuple (is_binary, content).
    """
    if isinstance(remote_path_info, tuple) and remote_path_info[0] == "symlink":
        link_target = remote_path_info[2]
        logger.debug(f"Processing symlink {remote_path_info[1]} with target: {link_target}")
        return (False, [f"SYMLINK: {link_target}"])
    full_path = remote_path_info[1] if isinstance(remote_path_info, tuple) else remote_path_info
    try:
        file_stat = sftp.stat(full_path)
        file_size = file_stat.st_size
    except Exception as e:
        logger.error(f"Error stating file {full_path}: {e}")
        return (None, None)
    # Use MD5 streaming for large files
    if file_size > LARGE_FILE_THRESHOLD:
        logger.info(f"File {full_path} is large ({file_size} bytes); using streaming MD5 comparison.")
        md5 = hashlib.md5()
        try:
            with sftp.open(full_path, 'rb') as f:
                while True:
                    chunk = f.read(4096)
                    if not chunk:
                        break
                    md5.update(chunk)
            return (True, md5.hexdigest())
        except Exception as e:
            logger.error(f"Error reading large file {full_path}: {e}")
            return (None, None)
    # Read the entire file into memory
    try:
        with sftp.open(full_path, 'rb') as remote_file:
            data = remote_file.read()
    except Exception as e:
        logger.error(f"Error reading file {full_path}: {e}")
        return (None, None)
    ext = posixpath.splitext(full_path)[1].lower()
    # If a specific handler exists for this file extension, use it.
    if ext in FILE_FORMAT_HANDLERS:
        return FILE_FORMAT_HANDLERS[ext](data, full_path)
    elif ext in ['.doc', '.xls']:
        logger.warning(f"File {full_path} is an older Office format; treating as binary.")
        return (True, data)
    # Check for binary content (null byte) and attempt text decoding.
    if b'\0' in data:
        logger.debug(f"File {full_path} detected as binary (null byte found).")
        return (True, data)
    else:
        try:
            text = data.decode('utf-8')
        except UnicodeDecodeError as e:
            logger.warning(f"UTF-8 decode error for {full_path}: {e}. Falling back to latin-1.")
            text = data.decode('latin-1')
        return (False, text.splitlines())

# --------------------------------------------------
# Process a Single File Comparison
# --------------------------------------------------
def process_file(rel_path, params):
    """
    Compares one file (by its relative path) between the two remote servers.
    Applies various exception filters and includes server labels in the output.
    """
    # Create server labels with IP addresses for clarity.
    server_a_label = f"Server A ({params['serverA']})"
    server_b_label = f"Server B ({params['serverB']})"
    filesA = params["filesA"]
    filesB = params["filesB"]
    sftpA = get_sftp_client_for(params["serverA"], params["userA"], params["keyA"], params["passA"], "sftpA")
    sftpB = get_sftp_client_for(params["serverB"], params["userB"], params["keyB"], params["passB"], "sftpB")

    # Apply exception filters:
    # 1. Folder Exceptions
    for folder in params["exception_folders"]:
        if rel_path.startswith(folder):
            record_exclusion(rel_path, "Folder Exception", filesA.get(rel_path), filesB.get(rel_path), sftpA, sftpB)
            return None
    # 2. Extension Exceptions
    if posixpath.splitext(rel_path)[1].lower() in params["exception_extensions"]:
        record_exclusion(rel_path, "Extension Exception", filesA.get(rel_path), filesB.get(rel_path), sftpA, sftpB)
        return None
    # 3. Exact File Exceptions
    base_name = posixpath.basename(rel_path)
    if base_name in params["file_exceptions"]:
        record_exclusion(rel_path, "Exact File Exception", filesA.get(rel_path), filesB.get(rel_path), sftpA, sftpB)
        return None
    # 4. Substring Exceptions
    for substr in params["file_substring_exceptions"]:
        if substr in base_name:
            record_exclusion(rel_path, f"Substring Exception: '{substr}'", filesA.get(rel_path), filesB.get(rel_path), sftpA, sftpB)
            return None
    # 5. Date Threshold Filtering
    dt_threshold = params["date_threshold"]
    if dt_threshold:
        skip_due_to_date = True
        if rel_path in filesA:
            try:
                mtime_a = datetime.fromtimestamp(sftpA.stat(filesA[rel_path][1]).st_mtime)
                if mtime_a >= dt_threshold:
                    skip_due_to_date = False
            except Exception as e:
                logger.error(f"Error getting mtime for {filesA[rel_path][1]}: {e}")
        if rel_path in filesB:
            try:
                mtime_b = datetime.fromtimestamp(sftpB.stat(filesB[rel_path][1]).st_mtime)
                if mtime_b >= dt_threshold:
                    skip_due_to_date = False
            except Exception as e:
                logger.error(f"Error getting mtime for {filesB[rel_path][1]}: {e}")
        if skip_due_to_date:
            record_exclusion(rel_path, "Modification Time Before Threshold", filesA.get(rel_path), filesB.get(rel_path), sftpA, sftpB)
            return None

    # If file passes all filters, perform the comparison.
    result = {}
    file_id = rel_path.replace('/', '_').replace(' ', '_').replace('.', '_')
    # Include server labels in the file title.
    result["id"] = file_id
    result["title"] = f"File: {rel_path} - Compared: {server_a_label} vs {server_b_label}"
    file_a = filesA.get(rel_path)
    file_b = filesB.get(rel_path)
    if file_a and file_b:
        # Read file contents from both servers.
        is_binary_a, contentA = get_file_content(sftpA, file_a)
        is_binary_b, contentB = get_file_content(sftpB, file_b)
        if is_binary_a is None or is_binary_b is None:
            result["message"] = "Error reading one of the files."
            result["category"] = "Error"
        elif is_binary_a != is_binary_b:
            result["message"] = (f"File type differs between {server_a_label} and {server_b_label} (one binary, one text).")
            result["category"] = "Mismatch"
        else:
            if is_binary_a:
                # Compare binary files using MD5 hash.
                hashA = contentA if isinstance(contentA, str) else hashlib.md5(contentA).hexdigest()
                hashB = contentB if isinstance(contentB, str) else hashlib.md5(contentB).hexdigest()
                if hashA == hashB:
                    result["message"] = "Binary file is identical."
                    result["category"] = "Identical"
                else:
                    result["message"] = (f"Binary file differs. MD5 {server_a_label}: {hashA}, {server_b_label}: {hashB}")
                    result["category"] = "Binary Difference"
            else:
                # Compare text files line by line.
                if contentA == contentB:
                    result["message"] = "The file is identical."
                    result["category"] = "Identical"
                else:
                    result["message"] = "Differences found:"
                    result["category"] = "Text Difference"
                    # Generate an HTML diff table using difflib.
                    diff_table = difflib.HtmlDiff(wrapcolumn=80).make_table(
                        contentA,
                        contentB,
                        fromdesc=f"{server_a_label}: {rel_path}",
                        todesc=f"{server_b_label}: {rel_path}",
                        context=True,
                        numlines=5
                    )
                    result["diff_table"] = diff_table
    elif file_a and not file_b:
        result["message"] = f"File exists only on {server_a_label}."
        result["category"] = "Server A Only"
    elif file_b and not file_a:
        result["message"] = f"File exists only on {server_b_label}."
        result["category"] = "Server B Only"
    return result

# --------------------------------------------------
# Compute Summary Statistics for the Comparison
# --------------------------------------------------
def compute_summary(results):
    """
    Computes summary statistics by counting the number of files in each comparison category.
    """
    summary = {}
    for r in results:
        cat = r.get("category", "Other")
        summary[cat] = summary.get(cat, 0) + 1
    return summary

# --------------------------------------------------
# Generate HTML Report for File Comparisons
# --------------------------------------------------
def generate_html_report(results, output_filename, report_title, summary, output_folder):
    """
    Generates a detailed HTML report for the file comparisons.
    Includes summary statistics, a filter dropdown, a table of contents, and each file's comparison result.
    """
    js_script = """
<script>
function filterCategory() {
  var selected = document.getElementById('catFilter').value;
  var containers = document.getElementsByClassName('file-container');
  for (var i = 0; i < containers.length; i++) {
    var cat = containers[i].getAttribute('data-category');
    containers[i].style.display = (selected == 'All' || cat == selected) ? 'block' : 'none';
  }
}
</script>
    """
    # Build the summary HTML section.
    summary_html = "<h2>Summary Statistics</h2><ul>"
    for cat, count in sorted(summary.items()):
        summary_html += f"<li>{cat}: {count}</li>"
    summary_html += "</ul>"
    # Build the filter dropdown for categories.
    categories = set(r.get("category", "Other") for r in results)
    filter_html = "<label for='catFilter'>Filter by category:</label> "
    filter_html += "<select id='catFilter' onchange='filterCategory()'><option>All</option>"
    for cat in sorted(categories):
        filter_html += f"<option>{cat}</option>"
    filter_html += "</select>"
    # Start the HTML document.
    html = f"""<!DOCTYPE html>
<html>
  <head>
    <meta charset="utf-8">
    <title>{report_title}</title>
    <link rel="stylesheet" href="https://stackpath.bootstrapcdn.com/bootstrap/4.5.2/css/bootstrap.min.css">
    <style>
      body {{
        padding: 20px;
        background-color: #f5f5f5;
      }}
      .container-report {{
        background-color: #ffffff;
        border: 1px solid #cccccc;
        border-radius: 8px;
        padding: 20px;
        margin-bottom: 20px;
        box-shadow: 0 2px 4px rgba(0,0,0,0.1);
      }}
      .file-container {{
        border: 1px solid #007bff;
        border-radius: 8px;
        margin-bottom: 15px;
        padding: 15px;
        background-color: #e9f7fe;
      }}
      .diff_table {{ width: 100%; border-collapse: collapse; }}
      .diff_table th, .diff_table td {{ border: 1px solid #ccc; padding: 4px; white-space: pre-wrap; }}
      .diff_header {{ background-color: #eaeaea; font-weight: bold; }}
      .diff_add {{ background-color: #a6f3a6; }}
      .diff_sub {{ background-color: #f3a6a6; }}
      .diff_chg {{ background-color: #f3e6a6; }}
    </style>
    {js_script}
  </head>
  <body>
    <div class="container-report">
      <h1>{report_title}</h1>
      {summary_html}
      {filter_html}
      <h2>Table of Contents</h2>
      <ul>
"""
    # Add a list item for each file comparison result.
    for item in results:
        html += f'<li><a href="#{item["id"]}">{item["title"]} ({item.get("category", "Other")})</a></li>\n'
    html += "</ul></div>\n"
    # Render each file's comparison details.
    for item in results:
        html += f'<div class="file-container" id="{item["id"]}" data-category="{item.get("category", "Other")}">'
        html += f'<h2>{item["title"]} ({item.get("category", "Other")})</h2>\n'
        html += f'<p>{item["message"]}</p>\n'
        if "diff_table" in item:
            html += item["diff_table"] + "\n"
        html += "</div>\n"
    # Add a link to view the process log.
    html += '<div class="container-report"><h2>Log File</h2>'
    html += f'<p><a href="/download?folder={encode_folder(output_folder)}&file=log.html" target="_blank">View Log Report</a></p>'
    html += "</div></body></html>"
    try:
        with open(output_filename, "w", encoding="utf-8") as f:
            f.write(html)
        logger.info(f"HTML report generated: {output_filename}")
    except Exception as e:
        logger.error(f"Error writing HTML report to {output_filename}: {e}")

# --------------------------------------------------
# Generate Exception Report (with Server Information)
# --------------------------------------------------
def generate_exception_report(exceptions_info, output_filename, report_title="Exception Report", server_a_label="", server_b_label=""):
    """
    Generates an HTML report detailing the files that were excluded from comparison,
    including reasons and modification times from both servers.
    """
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    # Include server information in the header.
    server_info_html = f"<p>Comparison between {server_a_label} and {server_b_label}</p>"
    rows_html = ""
    all_reasons = set()
    with exclusion_lock:
        if exclusion_records:
            for rec in exclusion_records:
                reason = rec["reason"]
                all_reasons.add(reason)
                rows_html += (
                    f"<tr>"
                    f"<td>{rec['file']}</td>"
                    f"<td class='reason-cell'>{reason}</td>"
                    f"<td>{rec['mtime_A']}</td>"
                    f"<td>{rec['mtime_B']}</td>"
                    f"</tr>\n"
                )
        else:
            rows_html = "<tr><td colspan='4'>No exclusions recorded.</td></tr>\n"
    distinct_reasons = sorted(all_reasons)
    dropdown_html = '<select id="reasonSelect" class="form-control" style="max-width:200px;" onchange="filterExclusions()">\n'
    dropdown_html += '<option value="">All</option>\n'
    for reason in distinct_reasons:
        escaped_reason = reason.replace('"', '&quot;')
        dropdown_html += f'<option value="{escaped_reason}">{reason}</option>\n'
    dropdown_html += '</select>'
    script = """
<script>
function filterExclusions() {
  var textInput = document.getElementById("searchBox");
  var filterText = textInput.value.toLowerCase();
  var reasonSelect = document.getElementById("reasonSelect");
  var selectedReason = reasonSelect.value.toLowerCase();
  var table = document.getElementById("exclusionTable");
  var trs = table.getElementsByTagName("tr");
  for (var i = 1; i < trs.length; i++) {
    var row = trs[i];
    if (!row.cells || row.cells.length < 2) {
      continue;
    }
    var reasonCellText = row.cells[1].innerText.toLowerCase();
    var rowText = row.innerText.toLowerCase();
    var passReason = (selectedReason === "") || (reasonCellText === selectedReason);
    var passText = (rowText.indexOf(filterText) !== -1);
    row.style.display = (passReason && passText) ? "" : "none";
  }
}
</script>
    """
    html = f"""<!DOCTYPE html>
<html>
  <head>
    <meta charset="utf-8">
    <title>{report_title}</title>
    <link rel="stylesheet" href="https://stackpath.bootstrapcdn.com/bootstrap/4.5.2/css/bootstrap.min.css">
    <style>
      body {{
        padding: 20px;
        background-color: #f5f5f5;
      }}
      .container-report {{
        background-color: #ffffff;
        border: 1px solid #cccccc;
        border-radius: 8px;
        padding: 20px;
        box-shadow: 0 2px 4px rgba(0,0,0,0.1);
      }}
      table {{
        width: 100%;
        border-collapse: collapse;
      }}
      th, td {{
        border: 1px solid #ccc;
        padding: 8px;
        text-align: left;
      }}
      th {{
        background-color: #eaeaea;
      }}
      .filter-area {{
        display: flex;
        flex-wrap: wrap;
        gap: 15px;
        align-items: center;
        margin-bottom: 15px;
      }}
    </style>
  </head>
  <body>
    <div class="container-report">
      <h1>{report_title}</h1>
      <p>Report generated on: {now_str}</p>
      {server_info_html}
      <h2>Configured Exception Filters</h2>
      <ul>
        <li>Folder Exceptions: {', '.join(sorted(exceptions_info.get("Folder Exceptions", [])) or ['None'])}</li>
        <li>Extension Exceptions: {', '.join(sorted(exceptions_info.get("Extension Exceptions", [])) or ['None'])}</li>
        <li>File Exceptions (Exact): {', '.join(sorted(exceptions_info.get("File Exceptions (Exact)", [])) or ['None'])}</li>
        <li>File Substring Exceptions: {', '.join(sorted(exceptions_info.get("File Substring Exceptions", [])) or ['None'])}</li>
      </ul>
      <h2>Exclusion Records</h2>
      <div class="filter-area">
        <div>
          <label for="searchBox"><strong>Search rows:</strong></label><br>
          <input type="text" id="searchBox" class="form-control" placeholder="Type text..." onkeyup="filterExclusions()" style="max-width:300px;">
        </div>
        <div>
          <label for="reasonSelect"><strong>Filter by Reason:</strong></label><br>
          {dropdown_html}
        </div>
      </div>
      <table class="table table-bordered" id="exclusionTable">
        <thead>
          <tr>
            <th>File</th>
            <th>Reason</th>
            <th>Modification Time (Server A)</th>
            <th>Modification Time (Server B)</th>
          </tr>
        </thead>
        <tbody>
          {rows_html}
        </tbody>
      </table>
    </div>
    {script}
  </body>
</html>
"""
    try:
        with open(output_filename, "w", encoding="utf-8") as f:
            f.write(html)
        logger.info(f"Exception report generated: {output_filename}")
    except Exception as e:
        logger.error(f"Error writing exception report to {output_filename}: {e}")

# --------------------------------------------------
# Generate Log Report (HTML Version of Process Log)
# --------------------------------------------------
def generate_log_report(output_folder):
    """
    Reads the process log and wraps it in an HTML page for easy viewing in a browser.
    """
    log_file = os.path.join(output_folder, "process.log")
    if not os.path.exists(log_file):
        return
    with open(log_file, "r", encoding="utf-8") as f:
        log_content = f.read()
    html = f"""<!DOCTYPE html>
<html>
  <head>
    <meta charset="utf-8">
    <title>Process Log</title>
    <link rel="stylesheet" href="https://stackpath.bootstrapcdn.com/bootstrap/4.5.2/css/bootstrap.min.css">
    <style>
      body {{ padding: 20px; background-color: #f8f9fa; }}
      pre {{ background-color: #ffffff; border: 1px solid #cccccc; padding: 15px; border-radius: 5px; }}
    </style>
  </head>
  <body>
    <div class="container">
      <h1>Process Log</h1>
      <pre>{log_content}</pre>
    </div>
  </body>
</html>
"""
    log_html_path = os.path.join(output_folder, "log.html")
    try:
        with open(log_html_path, "w", encoding="utf-8") as f:
            f.write(html)
        logger.info(f"Log report generated: {log_html_path}")
    except Exception as e:
        logger.error(f"Error writing log report: {e}")

# --------------------------------------------------
# URL Encoding Helper Functions for Downloads
# --------------------------------------------------
def encode_folder(folder):
    """Encodes a folder path to a URL-safe base64 string."""
    return base64.urlsafe_b64encode(folder.encode()).decode()

def decode_folder(encoded):
    """Decodes a URL-safe base64 string back to a folder path."""
    return base64.urlsafe_b64decode(encoded.encode()).decode()

# --------------------------------------------------
# Main Comparison Process
# --------------------------------------------------
def run_comparison(params):
    """
    Orchestrates the overall comparison process:
      - Connects to both remote servers.
      - Lists and filters files.
      - Processes each file comparison in parallel.
      - Generates HTML reports (comparison, exception, log).
      - Closes remote connections.
    """
    # Connect to Server A.
    sshA = connect_ssh(params["serverA"], params["userA"], params["keyA"], params["passA"])
    sftpA = sshA.open_sftp()
    # Connect to Server B.
    sshB = connect_ssh(params["serverB"], params["userB"], params["keyB"], params["passB"])
    sftpB = sshB.open_sftp()

    logger.info(f"Listing files in {params['remote_folderA']} on Server A ({params['serverA']})")
    filesA = list_remote_files(sftpA, params["remote_folderA"], follow_symlinks=params["follow_symlinks"])
    logger.info(f"Found {len(filesA)} files on Server A in {params['remote_folderA']}")
    logger.info(f"Listing files in {params['remote_folderB']} on Server B ({params['serverB']})")
    filesB = list_remote_files(sftpB, params["remote_folderB"], follow_symlinks=params["follow_symlinks"])
    logger.info(f"Found {len(filesB)} files on Server B in {params['remote_folderB']}")

    # If a compare list file is provided, filter the file dictionaries.
    if params["compare_list_file"]:
        try:
            with open(params["compare_list_file"], 'r', encoding='utf-8') as f:
                wanted = {line.strip() for line in f if line.strip()}
            logger.info(f"Compare list contains {len(wanted)} items.")
            filesA = {k: v for k, v in filesA.items() if k in wanted}
            filesB = {k: v for k, v in filesB.items() if k in wanted}
        except Exception as e:
            logger.error(f"Error reading compare list file {params['compare_list_file']}: {e}")

    # Save the file dictionaries in the params for later use.
    params["filesA"] = filesA
    params["filesB"] = filesB
    all_files = set(filesA.keys()).union(set(filesB.keys()))
    logger.info(f"Total unique file paths to compare: {len(all_files)}")

    # Process file comparisons in parallel using ThreadPoolExecutor.
    results = []
    max_workers = params.get("max_workers", 10)
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(process_file, rel_path, params): rel_path for rel_path in sorted(all_files)}
        for future in as_completed(futures):
            res = future.result()
            if res is not None:
                results.append(res)

    summary = compute_summary(results)
    server_a_label = f"Server A ({params['serverA']})"
    server_b_label = f"Server B ({params['serverB']})"

    # Transform summary keys to include server labels.
    transformed_summary = {}
    for key, count in summary.items():
        if key == "Server A Only":
            new_key = f"{server_a_label} Only"
        elif key == "Server B Only":
            new_key = f"{server_b_label} Only"
        else:
            new_key = key
        transformed_summary[new_key] = count

    logger.info(f"Summary statistics: {summary}")

    # Generate HTML reports for identical files and differences.
    output_folder = params["output_folder"]
    same_html = os.path.join(output_folder, "same_report.html")
    diff_html = os.path.join(output_folder, "diff_report.html")
    identical_count = sum(1 for r in results if r.get('category', '').lower() == 'identical')
    total_count = len(results)
    similarity_percentage = (100 * identical_count / total_count) if total_count > 0 else 0

    generate_html_report(
        [r for r in results if r.get("category", "").lower() == "identical"],
        same_html,
        f"Identical Files Report for {server_a_label} vs {server_b_label} (Similarity: {similarity_percentage:.2f}%)",
        transformed_summary,
        output_folder
    )
    generate_html_report(
        [r for r in results if r.get("category", "").lower() != "identical"],
        diff_html,
        f"Differences Report for {server_a_label} vs {server_b_label} (Similarity: {similarity_percentage:.2f}%)",
        transformed_summary,
        output_folder
    )

    # Generate the exception report.
    exceptions_report = os.path.join(output_folder, "exceptions_report.html")
    exceptions_info = {
        "Folder Exceptions": set(params["exception_folders"]),
        "Extension Exceptions": set(params["exception_extensions"]),
        "File Exceptions (Exact)": set(params["file_exceptions"]),
        "File Substring Exceptions": set(params["file_substring_exceptions"])
    }
    generate_exception_report(exceptions_info, exceptions_report, "Applied Exception Filters", server_a_label, server_b_label)

    # Generate the log report.
    generate_log_report(output_folder)

    # Close all SFTP and SSH connections.
    sftpA.close()
    sshA.close()
    sftpB.close()
    sshB.close()

    return output_folder

# --------------------------------------------------
# Load Configuration from a JSON File
# --------------------------------------------------
def load_config(config_path):
    """
    Loads a JSON configuration file and validates it.
    Returns the configuration dictionary.
    """
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            config = json.load(f)
        logger.info(f"Configuration loaded from {config_path}")
        validate_config(config)
        return config
    except Exception as e:
        logger.error(f"Error loading configuration file {config_path}: {e}")
        return {}

# --------------------------------------------------
# Command-Line Interface Entry Point (CLI mode)
# --------------------------------------------------
def main_cli():
    """
    Handles command-line invocation of the script.
    Parses command-line arguments and optional JSON configuration file.
    """
    args = sys.argv[1:]
    if "--web" in args:
        return  # Defer to the web interface if requested.
    if len(args) < 10:
        print("Not enough arguments.\n" + __doc__)
        sys.exit(1)
    config = {}
    # If the last argument is a config file, load it.
    if len(args) >= 19 and args[-1] not in ["None", ""]:
        possible_config = args[-1]
        if os.path.exists(possible_config):
            config = load_config(possible_config)
            args = args[:-1]
    # Build the parameters dictionary from command-line arguments and config.
    params = {
        "serverA": config.get("serverA", args[0]),
        "userA": config.get("userA", args[1]),
        "keyA": config.get("keyA") if config.get("keyA") is not None else args[2],
        "passA": config.get("passA") if config.get("passA") is not None else args[3],
        "remote_folderA": config.get("remote_folderA", args[4]),
        "serverB": config.get("serverB", args[5]),
        "userB": config.get("userB", args[6]),
        "keyB": config.get("keyB") if config.get("keyB") is not None else args[7],
        "passB": config.get("passB") if config.get("passB") is not None else args[8],
        "remote_folderB": config.get("remote_folderB", args[9]),
        "compare_list_file": config.get("compare_list_file", args[10] if len(args) >= 11 and args[10] not in ["None", ""] else None),
        "output_folder": config.get("output_folder", args[11] if len(args) >= 12 and args[11] not in ["None", ""] else f"compare_output_{datetime.now().strftime('%Y%m%d_%H%M%S')}"),
        "follow_symlinks": config.get("follow_symlinks", args[12].lower() == "true" if len(args) >= 13 else False),
        "exception_folders": load_exception_list(config.get("exception_folders", args[13] if len(args) >= 14 and args[13] not in ["None", ""] else [])),
        "exception_extensions": load_exception_list(config.get("exception_extensions", args[14] if len(args) >= 15 and args[14] not in ["None", ""] else [])),
        "date_threshold": config.get("date_threshold", args[15] if len(args) >= 17 and args[15] not in ["None", ""] else None),
        "file_exceptions": load_exception_list(config.get("file_exceptions", args[16] if len(args) >= 18 and args[16] not in ["None", ""] else [])),
        "file_substring_exceptions": load_exception_list(config.get("file_substring_exceptions", args[17] if len(args) >= 19 and args[17] not in ["None", ""] else [])),
        "max_workers": config.get("max_workers", 10)
    }
    # Parse date threshold if provided.
    if params["date_threshold"]:
        try:
            params["date_threshold"] = datetime.strptime(params["date_threshold"], "%Y-%m-%d %H:%M:%S")
        except Exception as e:
            logger.error(f"Error parsing date threshold: {e}")
            params["date_threshold"] = None
    # Create the output folder and setup logging.
    os.makedirs(params["output_folder"], exist_ok=True)
    setup_logging(params["output_folder"])
    logger.info(f"Folder Exceptions: {params['exception_folders']}")
    logger.info(f"Extension Exceptions: {params['exception_extensions']}")
    logger.info(f"File Exceptions (Exact): {params['file_exceptions']}")
    logger.info(f"File Substring Exceptions: {params['file_substring_exceptions']}")
    output_folder = run_comparison(params)
    print(f"Reports generated in folder: {output_folder}")

# --------------------------------------------------
# Web Interface Entry Point (Flask)
# --------------------------------------------------
def run_web_interface():
    """
    Launches the Flask web interface. Allows users to either fill in a form or upload a JSON configuration.
    """
    if Flask is None:
        print("Flask is not installed. Please install Flask to use the web interface.")
        sys.exit(1)
    app = Flask(__name__)
    # HTML form for input; includes JavaScript to disable form fields if a JSON config is uploaded.
    form_html = """
    <!DOCTYPE html>
    <html>
      <head>
        <meta charset="utf-8">
        <title>Remote Folder Comparison</title>
        <link rel="stylesheet" href="https://stackpath.bootstrapcdn.com/bootstrap/4.5.2/css/bootstrap.min.css">
        <style>
          body { padding-top: 30px; }
          .container { max-width: 800px; }
          .form-group textarea { height: 100px; }
          #loading { display: none; }
          .disabled-form { pointer-events: none; opacity: 0.5; }
        </style>
        <script>
          function disableForm() {
            document.querySelector("form").classList.add("disabled-form");
          }
          function showLoading() {
            document.getElementById('loading').style.display = 'block';
            disableForm();
          }
          function validateForm() {
            // If no JSON config is uploaded, validate required fields.
            var configInput = document.getElementsByName("config")[0];
            if (configInput.files.length === 0) {
              var requiredFields = ["serverA", "userA", "remote_folderA", "serverB", "userB", "remote_folderB"];
              for (var i = 0; i < requiredFields.length; i++) {
                var field = document.getElementsByName(requiredFields[i])[0];
                if (!field.value.trim()) {
                  alert("Please fill in the required field: " + requiredFields[i]);
                  return false;
                }
              }
            }
            return true;
          }
          document.addEventListener("DOMContentLoaded", function(){
            var configInput = document.getElementsByName("config")[0];
            var formFieldsDiv = document.getElementById("formFields");
            var requiredInputs = formFieldsDiv.querySelectorAll("[required]");
            configInput.addEventListener("change", function(){
              if (configInput.files.length > 0) {
                formFieldsDiv.style.display = "none";
                requiredInputs.forEach(function(elem) {
                  elem.disabled = true;
                  elem.removeAttribute("required");
                });
              } else {
                formFieldsDiv.style.display = "block";
                requiredInputs.forEach(function(elem) {
                  elem.disabled = false;
                  elem.setAttribute("required", "true");
                });
              }
            });
          });
        </script>
      </head>
      <body>
        <div class="container">
          <h1 class="mb-4">Remote Folder Comparison</h1>
          <form action="/run" method="post" enctype="multipart/form-data"
                onsubmit="if(validateForm()){showLoading(); return true;} else {return false;}">
            <div id="formFields">
              <h3>Server A Details</h3>
              <div class="form-group">
                <label>Server A: <span class="text-danger">*</span></label>
                <input type="text" name="serverA" class="form-control" required>
              </div>
              <div class="form-group">
                <label>User A: <span class="text-danger">*</span></label>
                <input type="text" name="userA" class="form-control" required>
              </div>
              <div class="form-group">
                <label>Key A (leave blank for None):</label>
                <input type="text" name="keyA" class="form-control">
              </div>
              <div class="form-group">
                <label>Password A (leave blank for None):</label>
                <input type="password" name="passA" class="form-control">
              </div>
              <div class="form-group">
                <label>Remote Folder A: <span class="text-danger">*</span></label>
                <input type="text" name="remote_folderA" class="form-control" required>
              </div>
              <hr>
              <h3>Server B Details</h3>
              <div class="form-group">
                <label>Server B: <span class="text-danger">*</span></label>
                <input type="text" name="serverB" class="form-control" required>
              </div>
              <div class="form-group">
                <label>User B: <span class="text-danger">*</span></label>
                <input type="text" name="userB" class="form-control" required>
              </div>
              <div class="form-group">
                <label>Key B (leave blank for None):</label>
                <input type="text" name="keyB" class="form-control">
              </div>
              <div class="form-group">
                <label>Password B (leave blank for None):</label>
                <input type="password" name="passB" class="form-control">
              </div>
              <div class="form-group">
                <label>Remote Folder B: <span class="text-danger">*</span></label>
                <input type="text" name="remote_folderB" class="form-control" required>
              </div>
              <hr>
              <h3>Comparison & Exception Settings</h3>
              <div class="form-group">
                <label>Compare List File (optional):</label>
                <input type="text" name="compare_list_file" class="form-control">
              </div>
              <div class="form-group">
                <label>Output Folder (optional):</label>
                <input type="text" name="output_folder" class="form-control">
              </div>
              <div class="form-group">
                <label>Follow Symlinks (true/false):</label>
                <input type="text" name="follow_symlinks" class="form-control" value="false">
              </div>
              <div class="form-group">
                <label>Exception Folders (enter one per line or file path):</label>
                <textarea name="exception_folders" class="form-control"></textarea>
              </div>
              <div class="form-group">
                <label>Exception Extensions (enter one per line or file path):</label>
                <textarea name="exception_extensions" class="form-control"></textarea>
              </div>
              <div class="form-group">
                <label>Date Threshold (YYYY-MM-DD HH:MM:SS, optional):</label>
                <input type="text" name="date_threshold" class="form-control">
              </div>
              <div class="form-group">
                <label>File Exceptions (Exact, one per line or file path):</label>
                <textarea name="file_exceptions" class="form-control"></textarea>
              </div>
              <div class="form-group">
                <label>File Substring Exceptions (one per line or file path):</label>
                <textarea name="file_substring_exceptions" class="form-control"></textarea>
              </div>
            </div>
            <hr>
            <div class="form-group">
              <label>Configuration File (JSON, optional):</label>
              <input type="file" name="config" class="form-control-file" accept=".json">
            </div>
            <div id="loading" class="alert alert-info">Processing... Please wait.</div>
            <button type="submit" id="submitBtn" class="btn btn-primary">Run Comparison</button>
          </form>
          <p class="mt-3"><strong>Note:</strong> If you upload a JSON configuration file, its values will override the form fields, and the form fields will be hidden/ignored.</p>
        </div>
      </body>
    </html>
    """
    @app.route("/", methods=["GET"])
    def index():
        return render_template_string(form_html)
    @app.route("/download", methods=["GET"])
    def download_file():
        encoded_folder = request.args.get("folder")
        file = request.args.get("file")
        if not encoded_folder or not file:
            return "Missing parameters", 400
        try:
            folder = decode_folder(encoded_folder)
        except Exception as e:
            return f"Error decoding folder: {e}", 400
        return send_from_directory(folder, file)
    @app.route("/view", methods=["GET"])
    def view_reports():
        folder = request.args.get("folder")
        if not folder or not os.path.exists(folder):
            return "<div class='container'><h2>Folder not found.</h2></div>"
        files = os.listdir(folder)
        encoded_folder = base64.urlsafe_b64encode(folder.encode()).decode()
        links = ""
        for file in files:
            links += f'<li><a href="/download?folder={encoded_folder}&file={file}" target="_blank">{file}</a></li>'
        return f"""
        <div class="container">
          <h2>Reports in {folder}</h2>
          <ul>{links}</ul>
          <p><a href="/" class="btn btn-secondary">Back to Form</a></p>
        </div>
        """
    @app.route("/run", methods=["POST"])
    def run():
        """
        Processes the submitted form or JSON configuration,
        then launches the comparison process and returns a result page.
        """
        json_uploaded = False
        config = {}
        if "config" in request.files and request.files["config"].filename != "":
            config_file = request.files["config"]
            if not config_file.filename.lower().endswith(".json"):
                return ("""
                <div class='container'>
                  <h2>Error: Only JSON files are allowed for the configuration.</h2>
                  <p><a href='/' class='btn btn-secondary'>Back</a></p>
                </div>
                """)
            temp_config_path = "temp_config.json"
            config_file.save(temp_config_path)
            try:
                config = load_config(temp_config_path)
            except Exception as e:
                os.remove(temp_config_path)
                return (f"""
                <div class='container'>
                  <h2>Error parsing JSON file: {e}</h2>
                  <p><a href='/' class='btn btn-secondary'>Back</a></p>
                </div>
                """)
            os.remove(temp_config_path)
            json_uploaded = True
            required_keys = ["serverA", "userA", "remote_folderA", "serverB", "userB", "remote_folderB"]
            missing_keys = [k for k in required_keys if not config.get(k)]
            if missing_keys:
                return (f"""
                <div class='container'>
                  <h2>Error: Missing required config keys in JSON: {', '.join(missing_keys)}</h2>
                  <p><a href='/' class='btn btn-secondary'>Back</a></p>
                </div>
                """)
        if json_uploaded:
            params = {
                "serverA": config.get("serverA"),
                "userA": config.get("userA"),
                "keyA": config.get("keyA"),
                "passA": config.get("passA"),
                "remote_folderA": config.get("remote_folderA"),
                "serverB": config.get("serverB"),
                "userB": config.get("userB"),
                "keyB": config.get("keyB"),
                "passB": config.get("passB"),
                "remote_folderB": config.get("remote_folderB"),
                "compare_list_file": config.get("compare_list_file"),
                "output_folder": config.get("output_folder") or f"compare_output_{datetime.now().strftime('%Y%m%d_%H%M%S')}",
                "follow_symlinks": config.get("follow_symlinks", False),
                "exception_folders": load_exception_list(config.get("exception_folders", [])),
                "exception_extensions": load_exception_list(config.get("exception_extensions", [])),
                "date_threshold": config.get("date_threshold"),
                "file_exceptions": load_exception_list(config.get("file_exceptions", [])),
                "file_substring_exceptions": load_exception_list(config.get("file_substring_exceptions", [])),
                "max_workers": config.get("max_workers", 10)
            }
        else:
            required_fields = ["serverA", "userA", "remote_folderA", "serverB", "userB", "remote_folderB"]
            missing = [field for field in required_fields if not request.form.get(field)]
            if missing:
                return f"""
                <div class='container'>
                  <h2>Error: Missing required fields: {', '.join(missing)}</h2>
                  <p><a href='/' class='btn btn-secondary'>Back</a></p>
                </div>
                """
            params = {
                "serverA": request.form.get("serverA"),
                "userA": request.form.get("userA"),
                "keyA": request.form.get("keyA"),
                "passA": request.form.get("passA"),
                "remote_folderA": request.form.get("remote_folderA"),
                "serverB": request.form.get("serverB"),
                "userB": request.form.get("userB"),
                "keyB": request.form.get("keyB"),
                "passB": request.form.get("passB"),
                "remote_folderB": request.form.get("remote_folderB"),
                "compare_list_file": request.form.get("compare_list_file"),
                "output_folder": request.form.get("output_folder") or f"compare_output_{datetime.now().strftime('%Y%m%d_%H%M%S')}",
                "follow_symlinks": (request.form.get("follow_symlinks", "false").lower() == "true"),
                "exception_folders": load_exception_list(request.form.get("exception_folders", "")),
                "exception_extensions": load_exception_list(request.form.get("exception_extensions", "")),
                "date_threshold": request.form.get("date_threshold"),
                "file_exceptions": load_exception_list(request.form.get("file_exceptions", "")),
                "file_substring_exceptions": load_exception_list(request.form.get("file_substring_exceptions", "")),
                "max_workers": 10
            }
        if params["date_threshold"]:
            try:
                params["date_threshold"] = datetime.strptime(params["date_threshold"], "%Y-%m-%d %H:%M:%S")
            except Exception as e:
                logger.error(f"Error parsing date threshold: {e}")
                params["date_threshold"] = None
        os.makedirs(params["output_folder"], exist_ok=True)
        setup_logging(params["output_folder"])
        output_folder = run_comparison(params)
        return f"""
        <div class="container">
          <h2>Comparison Complete</h2>
          <p>Reports generated in folder: {output_folder}</p>
          <p><a href="/view?folder={output_folder}" class="btn btn-primary">View Reports</a></p>
          <p><a href="/" class="btn btn-secondary">Back to Form</a></p>
        </div>
        """
    app.run(host="0.0.0.0", port=4900)

# --------------------------------------------------
# Main Entry Point
# --------------------------------------------------
if __name__ == "__main__":
    if "--web" in sys.argv:
        run_web_interface()
    else:
        main_cli()
