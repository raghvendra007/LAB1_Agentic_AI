NAME = "remember"
ARGS = {"fact": str, "source": str}
DESCRIPTION = ('args {"fact": "...", "source": "SG-13"}  Save a short fact in your own words (e.g. a rule you found). '
               'Saved facts are shown to you in every step, so you do not need to search for them again.')


def run(args, state):
    fact = f"{args['fact'].strip()} ({args['source'].strip()})"
    dropped = state.ctx.remember(fact)
    note = f" Memory full: dropped the oldest fact '{dropped}'." if dropped else ""
    return f"Saved. You have {len(state.ctx.facts)} facts.{note}", f"saved fact: {fact[:70]}"
