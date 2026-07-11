CypherX AI


The Operating System for Enterprise AI Agents



CypherX is a multi-tenant, language-agnostic, agentic platform for building, deploying, and orchestrating intelligent AI agents at production scale. It gives developers a unified, enterprise-grade foundation — authentication, LLM orchestration, memory, retrieval, safety, tool execution, and observability — so they can focus on building intelligence instead of infrastructure.


Table of Contents


Inspiration
What It Does
Key Features
Architecture
Tech Stack
Request Pipeline
Challenges We Ran Into
Accomplishments
What We Learned
Roadmap
Getting Started
Contributing
License



Inspiration

The rapid growth of Agentic AI has made it easier than ever to build intelligent applications, but creating production-ready AI systems remains a significant challenge. Developers often spend more time building infrastructure than building intelligence — integrating authentication, multiple LLM providers, retrieval systems, memory, safety mechanisms, tool execution, observability, and deployment pipelines before an agent can perform a single task.

We wanted to change that.

CypherX was inspired by the idea of providing a unified, enterprise-grade platform where developers can focus entirely on building intelligent agents while the platform handles scalability, security, orchestration, governance, and infrastructure. Instead of treating AI as a collection of disconnected services, we envisioned an operating system for AI agents that provides everything needed to build reliable, production-ready autonomous applications.

What It Does

CypherX AI is a multi-tenant, language-agnostic, agentic platform for building, deploying, and orchestrating intelligent agents. Agents can operate independently or collaborate through an orchestrator.

The platform authenticates agents (not end users), while end-user identity is managed externally. Every cross-service interaction is secured using RS256 JWT authentication, isolated per tenant through PostgreSQL Row-Level Security (RLS), and fully observable using W3C trace propagation, structured JSON logging, and Prometheus metrics.

Each Shared Core service is also a standalone, SaaS-ready product, allowing organizations to deploy only the services they need.

Key Features


 Autonomous AI Agent Runtime
 Long-term Semantic Memory
 Retrieval-Augmented Generation (RAG)
 Enterprise-grade Authentication & Authorization
 AI Guardrails for Input & Output Safety
 MCP-based Tool Integration
 Unified Multi-Provider LLM Gateway
 Real-time Monitoring & Distributed Tracing
 Multi-tenant SaaS Architecture
 Event-driven Microservices


Rather than rebuilding these capabilities for every AI application, developers can build directly on top of CypherX and deploy production-ready AI systems significantly faster.

Architecture

CypherX follows a contract-first microservices architecture, where every service is independently deployable and communicates through immutable OpenAPI and JSON Schema contracts.

Core Services

ServiceDescriptionStackAuthentication ServiceAgent identity, RS256 JWT issuance & validationKotlin, Spring BootLLM GatewayUnified interface across multiple LLM providersPython, FastAPIAgent Runtime (xAgent)Autonomous agent execution enginePython, FastAPIGuardrails ServiceInput/output safety and policy enforcementPython, FastAPIMemory ServiceLong-term semantic memoryPython, FastAPI, pgvectorRAG ServiceRetrieval-augmented generationPython, FastAPI, pgvectorMCP Tool RegistryTool discovery and execution via MCPPython / Node.js, FastifyBackend-for-Frontend (BFF)API aggregation layer for the dashboardNode.js, FastifyDashboardWeb console for managing agents and tenantsNext.js, React, TypeScript

Tech Stack

Backend


Python 3.12
FastAPI
Kotlin
Spring Boot
Node.js
Fastify


Infrastructure & Data


PostgreSQL + pgvector
Neon
Kafka (Redpanda)
Valkey
MinIO
Atlas


Cloud & Deployment


Docker
Kubernetes
AWS / EKS
Terraform
Helm
ArgoCD
Doppler


Networking & Security


Caddy
Kong
Istio
JWT (RS256)


Frontend


Next.js
React
TypeScript


Observability


OpenTelemetry
Prometheus
Grafana
Loki
Tempo


AI & Integrations


Anthropic API
OpenAI API
MCP (Model Context Protocol)
GitHub API


Request Pipeline

Every request flows through a secure, modular execution pipeline:

textAuthentication
      ↓
Guardrails
      ↓
Memory & RAG
      ↓
LLM Gateway
      ↓
Tool Execution (MCP)
      ↓
Event Publishing
      ↓
Observability
      ↓
Response

This architecture ensures security, reliability, scalability, and complete traceability across every agent interaction.

Challenges We Ran Into

Designing truly independent services
We wanted every service to be reusable and independently deployable while maintaining strict compatibility. This required adopting a contract-first development approach and carefully designing immutable APIs.

Secure multi-tenancy
Ensuring complete tenant isolation without sacrificing performance required implementing PostgreSQL Row-Level Security (RLS), JWT-based identity propagation, and Zero Trust service communication.

Unified LLM abstraction
Different LLM providers expose different APIs and response formats. We built a gateway capable of normalizing requests and responses while supporting multiple providers through a single interface.

Reliable agent orchestration
Building an agent runtime capable of safely coordinating memory retrieval, RAG, guardrails, LLM calls, and tool execution required designing a modular stage-based execution pipeline.

Production reliability
Distributed AI systems generate large numbers of asynchronous events. We adopted Kafka with the Transactional Outbox Pattern to eliminate dual-write problems and ensure reliable event delivery.

Accomplishments


Built a complete enterprise-ready Agentic AI platform.
Designed a fully modular contract-first microservices architecture.
Created a unified gateway supporting multiple LLM providers.
Implemented enterprise-grade security using Zero Trust principles.
Integrated Memory, RAG, Guardrails, and MCP tools into a single orchestration pipeline.
Added end-to-end observability using distributed tracing and centralized monitoring.
Designed every service to operate independently or as part of the larger ecosystem.
Built a scalable cloud-native foundation capable of supporting future AI workloads.

Documentation
https://drive.google.com/file/d/1RDXQZhR2plwuvasJ5jE790yvtA-YbBMH/view?usp=sharing

Video Link
https://youtu.be/u-w19OywweE

Pictures
https://drive.google.com/drive/u/0/folders/13k9m1oWxZ-riKIkd9g7S2TuOCcVpSwH1

What We Learned

This project taught us that building enterprise AI systems is far more than integrating language models.


Strong software architecture matters as much as model quality.
Contract-first development dramatically simplifies large distributed systems.
Security, governance, and tenant isolation must be designed from the beginning.
Reliable AI applications require orchestration, memory, retrieval, safety, and observability working together.
Event-driven architectures improve resilience and scalability for AI workloads.
The future of enterprise AI lies in platforms that combine intelligence, reliability, compliance, and operational excellence.


What's Next for CypherX

Our vision is to evolve CypherX into the Operating System for Enterprise AI.


Multi-agent collaboration and swarm orchestration
Visual workflow builder for agent pipelines
Marketplace for reusable AI agents and MCP tools
Native integrations with Slack, GitHub, Jira, Notion, and enterprise software
On-premise and hybrid deployment support
Intelligent LLM routing and cost optimization
Continuous evaluation, benchmarking, and monitoring
Advanced governance, compliance, and policy management
SDKs and APIs for developers building custom AI applications
Community ecosystem for extending the CypherX platform




Prerequisites


Docker & Docker Compose
Kubernetes cluster (for production deployment)
PostgreSQL 15+ with pgvector extension
Python 3.12+
Node.js 20+
Kotlin / JDK 17+ (for the Authentication Service)


Local Setup

bash# Clone the repository
git clone https://github.com/<your-org>/cypherx-ai.git
cd cypherx-ai

# Copy environment variables
cp .env.example .env

# Start core services locally
docker compose up -d

# Run database migrations
make migrate

# Start the dashboard
cd apps/dashboard
npm install
npm run dev

Deploying to Kubernetes

bashhelm install cypherx ./deploy/helm/cypherx \
  --namespace cypherx \
  --create-namespace \
  -f ./deploy/helm/values.yaml

Contributing

Contributions are welcome! Please open an issue to discuss significant changes before submitting a pull request.


Fork the repository
Create a feature branch (git checkout -b feature/my-feature)
Commit your changes
Push to the branch and open a Pull Request


License
Apache 2.0
