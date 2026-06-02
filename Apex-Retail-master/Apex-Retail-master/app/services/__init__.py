"""Application / service layer.

Use-cases live here. Services orchestrate repositories + domain rules and
expose plain Python types — they do NOT import FastAPI or Pydantic schemas
unless those types are part of the contract with the API layer.

A service should be unit-testable with a real (test) DB and no HTTP stack.
"""
