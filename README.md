# GhostFix AI

> Autonomous Verified Software Repair

GhostFix AI is an autonomous software repair agent that reproduces bugs, investigates repositories, generates candidate fixes, executes them in isolated environments, rejects failing patches, and produces verifiable evidence before a human approves the repair.

## Why GhostFix?

Modern AI coding assistants can generate code, but developers still need to determine whether a proposed fix actually solves the bug and avoids regressions.

GhostFix focuses on one principle:

**Don't just generate the fix. Prove it works.**

## Core Workflow

```text
Repository / Issue
        ↓
Bug Reproduction
        ↓
Repository Investigation
        ↓
Root-Cause Analysis
        ↓
Candidate Patch
        ↓
Sandbox Execution
        ↓
Test Verification
        ↓
Self-Correction
        ↓
Proof of Fix
        ↓
Human Approval
