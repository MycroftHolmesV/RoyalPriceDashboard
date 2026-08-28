# Contributing

Thanks for helping this tiny ship stay on course.

Before opening a pull request:

1. Keep browsing, pinning, watching, and history separate. Only an explicitly
   watched product may produce an alert.
2. Keep sailing dates in ISO 8601 (`YYYY-MM-DD`) inside the App.
3. Do not commit secrets, tokens, live catalogs, preferences, notification
   data, SQLite databases, or material copied from an App's `/data` volume.
4. Preserve `royal-price-dashboard/upstream-LICENSE` and update the Dockerfile
   checksum deliberately if the pinned upstream source changes.
5. Run:

   ```text
   cd royal-price-dashboard
   python -m unittest discover -s tests -v
   python -m py_compile server.py upstream_adapter.py tests/test_server.py
   node --check static/app.js
   ```

For changes to upstream catalog behavior, prefer a structured output contract
in the upstream project over parsing additional terminal presentation text.
