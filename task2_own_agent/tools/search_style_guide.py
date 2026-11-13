from rag import format_observation

NAME = "search_style_guide"
ARGS = {"query": str}
DESCRIPTION = ('args {"query": "..."}  Search the style guide (one topic per query). '
               'Returns matching rules with their document id, e.g. [SG-13].')


def run(args, state):
    if not args["query"].strip():
        raise ValueError('search_style_guide needs a non-empty "query".')
    chunks = state.rag.search(args["query"], state.k)
    observation = format_observation(chunks, max_tokens=1000)  # lab rule: at most 1,000 tokens
    ids = list(dict.fromkeys(c.doc_id for c in chunks if c.text in observation))
    return observation, f"found {', '.join(ids) or 'nothing'}"
