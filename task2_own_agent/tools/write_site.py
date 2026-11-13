import re

from common.embed_images import embed_images
from common.site_checker import check_site

NAME = "write_site"
ARGS = {"html": str}
DESCRIPTION = ("args {}  Put the COMPLETE page in a ```html code block right after your JSON. "
               "It is saved and checked; you get the checker report.")


def run(args, state):
    state.last_report = None
    state.version += 1
    html = re.sub(r"^\s*```[a-z]*\s*\n|\n?\s*```\s*$", "", args["html"])  # a code fence inside the string
    try:
        html = embed_images(html, state.content / "images")  # {{image:x.jpg}} -> base64
    except (FileNotFoundError, ValueError) as exc:
        return f"Error, page not saved: {exc}", f"page v{state.version} NOT saved: {str(exc)[:60]}"
    state.out.parent.mkdir(parents=True, exist_ok=True)
    state.out.write_text(html, encoding="utf-8")
    report = check_site(html, mode="full", member_names=state.members, file_size=len(html.encode("utf-8")))
    state.last_report = report
    failed = [r.name for r in report.failures()]
    n = len(report.results)
    short = f"page v{state.version}: {n - len(failed)}/{n} checks" + (f", failed: {', '.join(failed)}" if failed else ", all passed")
    return f"Saved. Checker report:\n{report.summary()}", short
