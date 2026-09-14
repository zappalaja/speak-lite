# Code reference snippets

Every `.py` file in this folder is a **developer-approved reference snippet**.
The RAG service embeds each file's leading docstring in a dedicated Chroma
collection (`code_snippets`); on every Code Mode data request, the top-matching
snippets are retrieved (POST `/snippets/search`) and injected into the prompt.
The LLM is instructed to base its answer on a matching snippet, keeping the
structure, API calls, and processing steps, and only filling in request-specific
values (variable, experiment, member, dates, bounds, units).

## Adding your own snippet

1. Create a `.py` file here with a descriptive filename
   (`ensemble_mean_timeseries.py`).
2. Start the file with a docstring saying **what kind of request it covers** —
   this is the text that gets embedded and matched against user queries, so
   include the words a user would use.
3. Write the code the way you want users to receive it: runnable, minimal,
   with placeholder values that are obviously meant to be replaced.
4. To reindex:  `curl -X POST http://localhost:8002/snippets/reindex`
   (or restart the RAG service, the app indexes on first search).

## General Notes for Developers

- Top-K retrieved per request is `SNIPPET_TOP_K` (chatbot `.env`, default 2).
- If the RAG service is down, code retrieval still works but free-forms
  without reference snippets.
