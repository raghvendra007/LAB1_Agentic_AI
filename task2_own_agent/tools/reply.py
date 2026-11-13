NAME = "reply"
ARGS = {"text": str}
DESCRIPTION = ('args {"text": "..."}  Answer the user and END your turn (for questions and chat; no page needed). '
               "Not accepted while a page you wrote in this turn fails its checks.")


def run(args, state):
    """The chat stop signal. finish = 'the page is done'; reply = 'here is my answer'."""
    if state.last_report is not None and not state.last_report.passed:
        return ("Rejected: the page you wrote in this turn fails checks. Fix it and finish, "
                "or explain the problem with say first."), "reply rejected: page failing"
    print(f"\n  agent: {args['text']}")
    state.done = True
    return "Delivered.", f'replied "{args["text"][:60]}"'
