"""Device-side modules — shipped verbatim into <ROOT>/bootstrap/ of every
delivered tree, so everything here is stdlib-only and must also work when the
files are executed loose (no package). Each module that needs a sibling uses:

    if __package__:
        from . import identifiers
    else:
        import identifiers

Spec: docs/STREAMLIT_DESKTOP_ATOMIC_UPDATE_IMPLEMENTATION_SPEC.md
"""
