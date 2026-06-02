"""Repositories — the only place the rest of the app talks to the DB.

A repository's job is to translate between schema/domain types and ORM rows.
Services should call repository methods, never query ORM directly.
"""
