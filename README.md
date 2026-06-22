# 🌉 AI Bridge Ecosystem

Welcome to the **AI Bridge** repository! This project provides a high-performance architecture for bridging synchronous AI workers into an asynchronous, scalable pipeline, effectively eliminating "polling fatigue" and streamlining agent workflows.

## 🌟 Overview

The AI Bridge is designed for developers and AI agents who need to manage long-running LLM tasks without blocking their primary execution threads. Instead of waiting in a fragile loop for a response, the system separates task initiation from result retrieval.

## 🛠️ Core Components

### 1. 🌉 The AI Bridge (`/ai_bridge`)
The heart of the project. This component acts as an orchestration layer that:
- **Async Transformation:** Converts synchronous worker calls into asynchronous tasks.
- **Task Management:** Tracks state and manages `taskIds` for retrieval.
- **Standardized API:** Provides a consistent interface via `/worker_call` and `/worker_check`.
- **Filesystem Guardrails:** `/worker_call` requires a frontmatter `working_directory` and validates it against the worker's configured write paths; Docker and the worker runtime enforce actual filesystem access.

### 2. 🐶 The AI Watchdog (`/ai_watchdog`)
A specialized utility designed to solve the "polling problem." The Watchdog is a blocking CLI tool that:
- **Monitors Tasks:** Tracks a specific `taskId` until it reaches a terminal state (Completed, Failed, etc.).
- **Zero Synthesis:** Delivers raw, authoritative JSON results directly from the bridge.
- **Agent-First Design:** Specifically built to be called by AI agents in background processes for maximum efficiency.

## 🚀 Getting Started

Please refer to the detailed documentation within each component's directory:
- See `/ai_bridge/README.md` for core installation and API details.
- See `/ai_watchdog/README.md` for configuration and usage of the watchdog utility.

---
*Built for durability, precision, and architectural elegance.*
