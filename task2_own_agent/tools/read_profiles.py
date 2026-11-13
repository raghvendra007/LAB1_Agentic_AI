NAME = "read_profiles"
ARGS = {}
DESCRIPTION = "args {}  Get the profiles of all team members and the list of their image files."


def run(args, state):
    files = sorted(p for p in state.content.glob("*.md") if not p.name.startswith("_"))
    images = sorted(p.name for p in (state.content / "images").glob("*") if p.is_file())
    texts = "\n\n".join(f"=== {p.name} ===\n{p.read_text(encoding='utf-8')}" for p in files)
    observation = f"{texts}\n\nImage files (use as {{{{image:<file>}}}}): {', '.join(images) or 'none'}"
    return observation, f"{len(files)} profiles, images: {', '.join(images)}"
