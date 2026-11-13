NAME = "say"
ARGS = {"text": str}
DESCRIPTION = 'args {"text": "..."}  Show a short message to the user (progress, a question). The work goes on.'


def run(args, state):
    print(f"  agent says: {args['text']}")
    return "Shown to the user.", f'said "{args["text"][:60]}"'
