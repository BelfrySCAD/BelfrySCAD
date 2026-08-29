"""openscad_docsgen's markdown image generator, vendored.

`MarkdownImageGen` below is upstream's, unchanged: it drives the same
image_manager/log_manager/errorlog/filehashes this package already
provides, so the markdown it writes is what openscad-mdimggen writes.
Only the entry point differs -- it is `belfryscad --mdimggen`, and it
returns an exit code rather than calling sys.exit.

BOSL2 uses this for its tutorials (`tutorials/*.md` -> `Tutorial-*.md`),
in two of its four CI workflows.
"""

from __future__ import print_function

import os
import posixpath
import sys
import glob
import os.path
import argparse
import platform

from .errorlog import errorlog, ErrorLog
from .imagemanager import image_manager
from .logmanager import log_manager
from .filehashes import FileHashes
from . import EXIT_FAILURE


class MarkdownImageGen(object):
    HASHFILE = ".source_hashes"

    def __init__(self, opts):
        self.opts = opts
        self.filehashes = FileHashes(os.path.join(self.opts.docs_dir, self.HASHFILE))

    def img_started(self, req):
        print("  {}... ".format(os.path.basename(req.image_file)), end='')
        sys.stdout.flush()

    def img_completed(self, req):
        if req.success:
            if req.status == "SKIP":
                print()
            else:
                print(req.status)
            sys.stdout.flush()
            return
        out = "\n\n"
        for line in req.echos:
            out += line + "\n"
        for line in req.warnings:
            out += line + "\n"
        for line in req.errors:
            out += line + "\n"
        out += "//////////////////////////////////////////////////////////////////////\n"
        out += "// LibFile: {}  Line: {}  Image: {}\n".format(
            req.src_file, req.src_line, os.path.basename(req.image_file)
        )
        out += "//////////////////////////////////////////////////////////////////////\n"
        for line in req.script_lines:
            out += line + "\n"
        out += "//////////////////////////////////////////////////////////////////////\n"
        errorlog.add_entry(req.src_file, req.src_line, out, ErrorLog.FAIL)
        sys.stderr.flush()

    def log_completed(self, req):
        if not req.success:
            out = "\n".join(req.errors + req.warnings)
            errorlog.add_entry(req.src_file, req.src_line, out, ErrorLog.FAIL)

    def processFiles(self, srcfiles):
        opts = self.opts
        image_root = os.path.join(opts.docs_dir, opts.image_root)
        for infile in srcfiles:
            fileroot = os.path.splitext(os.path.basename(infile))[0]
            outfile = os.path.join(opts.docs_dir, opts.file_prefix + fileroot + ".md")
            print(outfile)
            sys.stdout.flush()

            out = []
            log_requests = []
            with open(infile, "r") as f:
                script = []
                extyp = ""
                in_script = False
                imgnum = 0
                show_script = True
                linenum = -1
                for line in f.readlines():
                    linenum += 1
                    line = line.rstrip("\n")
                    if line.startswith("```openscad-log"):
                        in_script = True
                        is_log_block = True
                        script = []                        
                    elif line.startswith("```openscad"):
                        in_script = True;
                        is_log_block = False
                        if "-" in line and not line.startswith("```openscad-log"):
                            extyp = line.split("-")[1]
                        else:
                            extyp = ""
                        show_script = "ImgOnly" not in extyp
                        script = []
                        imgnum = imgnum + 1
                    elif in_script:
                        if line == "```":
                            in_script = False
                            if is_log_block:
                                req = log_manager.new_request(
                                    infile, linenum, script,
                                    completion_cb=self.log_completed,
                                    verbose=True
                                )
                                log_manager.process_requests()
                                out.append("```log")
                                if req.success and req.echos:
                                    out.extend(req.echos)
                                else:
                                    out.append("No log output generated.")
                                out.append("```")    
                            else:    
                                if opts.png_animation:
                                    fext = "png"
                                elif any(x in extyp for x in ("Anim", "Spin")):
                                    fext = "gif"
                                else:
                                    fext = "png"
                                fname = "{}_{}.{}".format(fileroot, imgnum, fext)
                                # posixpath, not os.path -- this is a
                                # markdown URL. See blocks.py's own note.
                                img_rel_url = posixpath.join(
                                    opts.image_root.replace(os.sep, "/"), fname)
                                imgfile = os.path.join(opts.docs_dir, *img_rel_url.split("/"))
                                image_manager.new_request(
                                    fileroot+".md", linenum,
                                    imgfile, script, extyp,
                                    default_colorscheme=opts.colorscheme,
                                    starting_cb=self.img_started,
                                    completion_cb=self.img_completed,
                                    verbose=opts.verbose
                                )
                                if show_script:
                                    out.append("```openscad")
                                    for line in script:
                                        if not line.startswith("--"):
                                            out.append(line)
                                    out.append("```")
                                out.append("![Figure {}]({})".format(imgnum, img_rel_url))
                            show_script = True
                            extyp = ""
                            is_log_block = False
                        else:
                            script.append(line)
                    else:
                        out.append(line)

            if not opts.test_only:
                with open(outfile, "w") as f:
                    for line in out:
                        print(line, file=f)

            has_changed = self.filehashes.is_changed(infile)
            if opts.force or opts.test_only or has_changed:
                image_manager.process_requests(test_only=opts.test_only)
                log_manager.process_requests(test_only=opts.test_only)
            image_manager.purge_requests()
            log_manager.purge_requests()

            if errorlog.file_has_errors(infile):
                self.filehashes.invalidate(infile)
            self.filehashes.save()


def _rc_defaults(path=".openscad_mdimggen_rc"):
    """The rc file's settings, or {} if it isn't there.

    Kept as real YAML (upstream's format, and a file this project does not
    own) rather than hand-parsed: `source_files` is documented as either a
    string or a block list, and quietly misreading someone's valid config
    is worse than the dependency.
    """
    import yaml
    if not os.path.exists(path):
        return {}
    with open(path, "r") as f:
        data = yaml.safe_load(f)
    return data if isinstance(data, dict) else {}


def main(argv=None):
    """Returns a process exit code. `argv` is everything after --mdimggen."""
    defaults = _rc_defaults()
    parser = argparse.ArgumentParser(
        prog="belfryscad --mdimggen",
        description="Render the openscad code blocks in markdown files to "
                    "images. A drop-in replacement for openscad-mdimggen "
                    "that renders through BelfrySCAD's own evaluator "
                    "instead of the OpenSCAD binary.")
    parser.add_argument('-D', '--docs-dir', default=defaults.get("docs_dir", "docs"),
                        help='The directory to put generated documentation in.')
    parser.add_argument('-P', '--file-prefix', default=defaults.get("file_prefix", ""),
                        help='The prefix to put in front of each output markdown file.')
    parser.add_argument('-T', '--test-only', action="store_true",
                        help="If given, don't generate images, but do try executing the scripts.")
    parser.add_argument('-I', '--image_root', default=defaults.get("image_root", "images"),
                        help='The directory to put generated images in.')
    parser.add_argument('-f', '--force', action="store_true",
                        help='If given, force regeneration of images.')
    parser.add_argument('-a', '--png-animation', action="store_true", default=True,
                        help='Accepted for compatibility; APNG is always used.')
    parser.add_argument('-v', '--verbose', help='Verbose progress output', action="store_true")
    parser.add_argument('-C', '--colorscheme', default=defaults.get("ColorScheme", "Cornfield"),
                        help='The color scheme for rendering images (e.g., Tomorrow).')
    parser.add_argument('srcfiles', nargs='*', help='List of input markdown files.')
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    # Animations are APNG here, never GIF -- same reason as --docsgen (see
    # png_writer.write_apng). Naming a file .gif and filling it with APNG
    # bytes would be worse than ignoring the setting.
    if defaults.get("png_animations") is False:
        print("belfryscad: png_animations is always on -- GIF animations are "
              "not supported; writing APNG instead.", file=sys.stderr)
    args.png_animation = True

    if not args.srcfiles:
        srcfiles = defaults.get("source_files", [])
        if isinstance(srcfiles, str):
            args.srcfiles = glob.glob(srcfiles)
        elif isinstance(srcfiles, list):
            args.srcfiles = []
            for srcfile in srcfiles:
                if isinstance(srcfile, str):
                    args.srcfiles.extend(glob.glob(srcfile))
    elif platform.system() == 'Windows':
        args.srcfiles = [file for src_file in args.srcfiles for file in glob.glob(src_file)]

    if not args.srcfiles:
        print("No files to parse.  Aborting.", file=sys.stderr)
        return EXIT_FAILURE

    try:
        MarkdownImageGen(args).processFiles(args.srcfiles)
    except OSError as e:
        print(e)
        return EXIT_FAILURE
    except KeyboardInterrupt:
        print(" Aborting.", file=sys.stderr)
        return EXIT_FAILURE
    finally:
        from .runner import runner
        runner.close()

    if errorlog.has_errors:
        print("WARNING: Errors encountered.", file=sys.stderr)
        return EXIT_FAILURE
    return 0


if __name__ == "__main__":
    sys.exit(main())


# vim: expandtab tabstop=4 shiftwidth=4 softtabstop=4 nowrap
