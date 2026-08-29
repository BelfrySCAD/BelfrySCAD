"""BelfrySCAD's build of openscad_docsgen.

parser.py, blocks.py, errorlog.py, filehashes.py, utils.py and the target_*
modules are openscad_docsgen's, vendored unchanged. Only the two modules
that ran OpenSCAD are ours -- imagemanager.py and logmanager.py -- so the
docsgen block syntax, the validation rules and the generated markdown stay
byte-for-byte what openscad-docsgen produces, while Examples and Figures
render through BelfrySCAD's evaluator instead of a subprocess per image.

Entry point: `belfryscad --docsgen [options] [srcfiles...]`, which takes
the same options as the openscad-docsgen command it replaces.
"""
from __future__ import annotations

import argparse
import glob
import os
import os.path
import platform
import sys

from .errorlog import ErrorLog, errorlog
from .logmanager import log_manager
from .parser import DocsGenParser, DocsGenException
from .target import default_target, target_classes

#: What upstream's `sys.exit(-1)` becomes at the shell. Matched exactly so a
#: caller testing for a specific code, not just non-zero, behaves the same.
EXIT_FAILURE = 255


class Options:
    """The settings DocsGenParser reads. Built from argparse for the CLI,
    or constructed directly (see default_options) for the GUI's preview."""

    def __init__(self, args):
        self.files = args.srcfiles
        self.target_profile = args.target_profile
        self.project_name = args.project_name
        self._docs_dir_locked = False
        self.docs_dir = args.docs_dir.rstrip("/")
        self.quiet = args.quiet
        self.force = args.force
        self.strict = args.strict
        self.test_only = args.test_only
        self.gen_imgs = not args.no_images
        self.gen_files = args.gen_files
        self.gen_toc = args.gen_toc
        self.gen_index = args.gen_index
        self.gen_topics = args.gen_topics
        self.gen_glossary = args.gen_glossary
        self.gen_cheat = args.gen_cheat
        self.gen_sidebar = args.gen_sidebar
        self.report = args.report
        self.dump_tree = args.dump_tree
        self.verbose = args.verbose
        self.enabled_features = [item.strip() for item in args.enabled_features.split(",")]
        self.sidebar_header = []
        self.sidebar_middle = []
        self.sidebar_footer = []
        self.update_target()

    @property
    def docs_dir(self):
        return self._docs_dir

    @docs_dir.setter
    def docs_dir(self, value):
        # Ignored once locked -- see lock_docs_dir.
        if not self._docs_dir_locked:
            self._docs_dir = value

    def lock_docs_dir(self, path: str):
        """Pin the output directory against the rc file's DocsDirectory.

        Needed by the GUI preview, which must write its images to a cache
        directory and never into the project's real docs tree. Setting
        docs_dir once is not enough: DocsGenParser.parse_file re-reads the
        whole rc file through _reset_header_defs on EVERY file it parses, so
        an unlocked override survives only until the next one. Caught by
        finding a stray BOSL2.wiki/ directory full of preview images in the
        working directory.
        """
        self._docs_dir_locked = False
        self.docs_dir = path
        self._docs_dir_locked = True
        self.update_target()

    @property
    def png_animation(self):
        """Always True. Animations are written as APNG because writing GIF
        would mean an LZW encoder and a colour quantiser for what is
        already a 24-bit render (see png_writer.write_apng). The setter is
        kept because parser.py assigns to this from a source file's
        `UsePNGAnimations:` line."""
        return True

    @png_animation.setter
    def png_animation(self, value):
        if not value:
            print("belfryscad: UsePNGAnimations/-a is always on -- "
                  "GIF animations are not supported; writing APNG instead.",
                  file=sys.stderr)

    def set_target(self, targ):
        if targ not in target_classes:
            return False
        self.target_profile = targ
        return True

    def update_target(self):
        self.target = target_classes[self.target_profile](
            project_name=self.project_name,
            docs_dir=self.docs_dir,
        )


def default_options(**overrides):
    """An Options with every CLI default applied, for callers that have no
    argparse namespace (the GUI docs preview). Keyword overrides are
    applied as plain attributes afterwards."""
    ns = _build_parser().parse_args([])
    opts = Options(ns)
    for key, value in overrides.items():
        setattr(opts, key, value)
    opts.update_target()
    return opts


def processFiles(opts):
    docsgen = DocsGenParser(opts)
    # DocsGenParser may change opts settings, based on the _rc file.

    if not opts.files:
        opts.files = glob.glob("*.scad")
    elif platform.system() == "Windows":
        opts.files = [file for src_file in opts.files for file in glob.glob(src_file)]

    fail = False
    for infile in opts.files:
        if not os.path.exists(infile):
            print("{} does not exist.".format(infile))
            fail = True
        elif not os.path.isfile(infile):
            print("{} is not a file.".format(infile))
            fail = True
        elif not os.access(infile, os.R_OK):
            print("{} is not readable.".format(infile))
            fail = True
    if fail:
        return EXIT_FAILURE

    docsgen.parse_files(opts.files, False)

    if opts.dump_tree:
        docsgen.dump_full_tree()
    log_manager.process_requests(test_only=opts.test_only)

    if opts.gen_files or opts.test_only:
        docsgen.write_docs_files()
    if opts.gen_toc:
        docsgen.write_toc_file()
    if opts.gen_index:
        docsgen.write_index_file()
    if opts.gen_topics:
        docsgen.write_topics_file()
    if opts.gen_glossary:
        docsgen.write_glossary_file()
    if opts.gen_cheat:
        docsgen.write_cheatsheet_file()
    if opts.gen_sidebar:
        docsgen.write_sidebar_file()

    if opts.report:
        errorlog.write_report()
    if errorlog.has_errors:
        print("WARNING: Errors encountered.", file=sys.stderr)
        return EXIT_FAILURE
    return 0


def _build_parser():
    parser = argparse.ArgumentParser(
        prog="belfryscad --docsgen",
        description="Generate OpenSCAD library documentation from docsgen "
                    "comments. A drop-in replacement for openscad-docsgen "
                    "that renders Examples and Figures with BelfrySCAD's "
                    "own evaluator instead of the OpenSCAD binary.")
    parser.add_argument('-D', '--docs-dir', default="docs",
                        help='The directory to put generated documentation in.')
    parser.add_argument('-T', '--test-only', action="store_true",
                        help="If given, don't generate images, but do try executing the scripts.")
    parser.add_argument('-q', '--quiet', action="store_true",
                        help="Suppress printing of progress data.")
    parser.add_argument('-S', '--strict', action="store_true",
                        help="If given, require File/LibFile and Section headers.")
    parser.add_argument('-f', '--force', action="store_true",
                        help='If given, force regeneration of images.')
    parser.add_argument('-n', '--no-images', action="store_true",
                        help='If given, skips image generation.')
    parser.add_argument('-m', '--gen-files', action="store_true",
                        help='If given, generate documents for each source file.')
    parser.add_argument('-i', '--gen-index', action="store_true",
                        help='If given, generate AlphaIndex.md file.')
    parser.add_argument('-I', '--gen-topics', action="store_true",
                        help='If given, generate Topics.md topics index file.')
    parser.add_argument('-t', '--gen-toc', action="store_true",
                        help='If given, generate TOC.md table of contents file.')
    parser.add_argument('-g', '--gen-glossary', action="store_true",
                        help='If given, generate Glossary.md file.')
    parser.add_argument('-c', '--gen-cheat', action="store_true",
                        help='If given, generate CheatSheet.md file with all Usage lines.')
    parser.add_argument('-s', '--gen_sidebar', action="store_true",
                        help="If given, generate _Sidebar.md file index.")
    parser.add_argument('-a', '--png-animation', action="store_true",
                        help='Accepted for compatibility; APNG is always used.')
    parser.add_argument('-P', '--project-name',
                        help='If given, sets the name of the project to be shown in titles.')
    parser.add_argument('-r', '--report', action="store_true",
                        help='If given, write all warnings and errors to docsgen_report.json')
    parser.add_argument('-d', '--dump-tree', action="store_true",
                        help='If given, dumps the documentation tree for debugging.')
    parser.add_argument('-p', '--target-profile', choices=target_classes.keys(), default=default_target,
                        help='Sets the output target profile.  Defaults to "{}"'.format(default_target))
    parser.add_argument('-e', '--enabled_features', default='',
                        help='Accepted for compatibility; this evaluator has no optional features.')
    parser.add_argument('-v', '--verbose', help='Verbose progress output', action="store_true")
    parser.add_argument('srcfiles', nargs='*', help='List of input source files.')
    return parser


def main(argv=None):
    """Returns a process exit code. `argv` is everything after --docsgen."""
    opts = Options(_build_parser().parse_args(sys.argv[1:] if argv is None else argv))
    try:
        return processFiles(opts)
    except DocsGenException as e:
        print(e)
        return EXIT_FAILURE
    except OSError as e:
        print(e)
        return EXIT_FAILURE
    except KeyboardInterrupt:
        print(" Aborting.", file=sys.stderr)
        return EXIT_FAILURE
    finally:
        from .runner import runner
        runner.close()


if __name__ == "__main__":
    sys.exit(main())
