"""ECHO capture for docsgen `Log:` blocks -- BelfrySCAD's replacement for
openscad_docsgen's logmanager, which shelled out to the OpenSCAD binary
with `--export-format=echo` and scraped stdout.

Same class and method names as the original, so the vendored parser.py and
blocks.py import it unchanged. The evaluator's echo_fn hands us the ECHO
lines directly, so there is no subprocess, no temp-file scraping and no
10-second timeout to worry about.
"""
from __future__ import annotations

import os
import sys

from .errorlog import errorlog, ErrorLog
from .runner import runner


class LogRequest:
    def __init__(self, src_file, src_line, script_lines,
                 starting_cb=None, completion_cb=None, verbose=False):
        self.src_file = src_file
        self.src_line = src_line
        # A leading "--" marks a line that runs but is not shown in the
        # docs; strip the marker before running it (as upstream does).
        self.script_lines = [
            line[2:] if line.startswith("--") else line
            for line in script_lines
        ]
        self.starting_cb = starting_cb
        self.completion_cb = completion_cb
        self.verbose = verbose

        self.complete = False
        self.status = "INCOMPLETE"
        self.success = False
        self.cmdline = []
        self.return_code = None
        self.stdout = []
        self.stderr = []
        self.echos = []
        self.warnings = []
        self.errors = []

    def starting(self):
        if self.starting_cb:
            self.starting_cb(self)

    def completed(self, status, result=None):
        self.complete = True
        self.status = status
        self.success = (status == "SUCCESS")
        self.return_code = 0 if self.success else -1
        if result is not None:
            self.echos = result.echos
            self.warnings = result.warnings
            self.errors = result.errors
            self.stdout = list(result.echos)
            self.stderr = result.warnings + result.errors
        if self.completion_cb:
            self.completion_cb(self)


class LogManager:
    def __init__(self):
        self.requests = []
        self.test_only = False
        # The GUI preview parses a file's neighbours to resolve cross-file
        # links. Their Log blocks must not run: they belong to files the
        # user is not looking at, and running them means executing scripts.
        self.enabled = True

    def purge_requests(self):
        self.requests = []

    def new_request(self, src_file, src_line, script_lines,
                    starting_cb=None, completion_cb=None, verbose=False):
        req = LogRequest(src_file, src_line, script_lines,
                         starting_cb, completion_cb, verbose=verbose)
        self.requests.append(req)
        return req

    def process_request(self, req):
        req.starting()
        src_dir = os.path.dirname(os.path.abspath(req.src_file)) or "."
        try:
            result = runner.run(req.script_lines, src_dir,
                                hard_warnings=self.test_only,
                                generate=not self.test_only)
        except Exception as e:
            req.completed("FAIL")
            req.errors = [str(e)]
            errorlog.add_entry(req.src_file, req.src_line,
                               "Script evaluation failed: {}".format(e), ErrorLog.FAIL)
            return
        # A Log block only wants echo output; producing no geometry is
        # normal and must not count as a failure, so unlike an image
        # request this checks errors alone.
        req.completed("SUCCESS" if not result.errors else "FAIL", result)

    def process_requests(self, test_only=False):
        self.test_only = test_only
        for req in self.requests:
            if self.enabled:
                self.process_request(req)
            else:
                req.completed("SKIP")
        self.requests = []


log_manager = LogManager()
