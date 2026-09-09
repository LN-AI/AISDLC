import functools
import logging
import time
from typing import Dict, Any, List, Optional
from backend.agents.requirement_intelligence.requirement_reviewer import RequirementReviewAgent
from backend.agents.architecture_intelligence.architecture_agent import ArchitectureAgent
from backend.agents.developer_intelligence.code_reviewer import CodeReviewAgent
from backend.agents.developer_intelligence.codegen_agent import CodeGenAgent
from backend.agents.developer_intelligence.ai_provenance_agent import AICodeProvenanceAgent
from backend.agents.developer_intelligence.migration_agent import MigrationAgent
from backend.agents.developer_intelligence.autofix_agent import AutoFixAgent
from backend.agents.security_intelligence.sensitive_data_agent import SensitiveDataAgent
from backend.agents.security_intelligence.prompt_safety import PromptSafetyAgent
from backend.agents.security_intelligence.compliance_mapper import ComplianceMapperAgent
from backend.agents.security_intelligence.vapt_agent import VAPTAgent
from backend.agents.security_intelligence.security_review_agent import SecurityReviewAgent
from backend.agents.security_intelligence.cfr21_agent import CFR21VerificationAgent
from backend.agents.security_intelligence.gxp_document_agent import GxPDocumentAgent
from backend.agents.security_intelligence.gxp_generator_agent import GxPGeneratorAgent
from backend.agents.operations_intelligence.issue_triage_agent import IssueTriageAgent
from backend.agents.governance_intelligence.release_gate import ReleaseGateAgent
from backend.agents.governance_intelligence.report_generator import ReportGeneratorAgent
from backend.agents.qa_intelligence.test_scenario_agent import TestScenarioAgent
from backend.agents.qa_intelligence.test_case_agent import TestCaseAgent
from backend.services.github_service import GitHubService
from backend.services.knowledge_base import knowledge_base
from backend.services import cost_optimization_service
from backend import config, database
from backend.workflows import shared as workflow_shared
from backend.workflows import autofix_pr as autofix_pr_workflow
from backend.workflows import codegen_scaffold as codegen_scaffold_workflow
from backend.workflows import full_review as full_review_workflow
from backend.workflows import issue_triage as issue_triage_workflow
from backend.workflows import issue_triage_batch as issue_triage_batch_workflow
from backend.workflows import migration_execute as migration_execute_workflow
from backend.workflows import modernization_pr as modernization_pr_workflow
from backend.workflows import release_gate as release_gate_workflow
from backend.workflows import review_pr as review_pr_workflow
from backend.workflows import standalone_report as standalone_report_workflow

logger = logging.getLogger(__name__)


def _log_call(method):
    """Logs entry, duration, review_id, and failures for every orchestrator method —
    the one chokepoint every route/agent invocation in the app passes through."""
    @functools.wraps(method)
    def _wrapper(self, *args, **kwargs):
        name = method.__name__
        project_name = kwargs.get("project_name") or (args[-1] if args and isinstance(args[-1], str) else "")
        start = time.perf_counter()
        logger.info("orchestrator.%s started (project=%s)", name, project_name)
        try:
            result = method(self, *args, **kwargs)
        except Exception:
            logger.exception(
                "orchestrator.%s failed after %.1fms (project=%s)",
                name, (time.perf_counter() - start) * 1000, project_name,
            )
            raise
        duration_ms = (time.perf_counter() - start) * 1000
        review_id = result.get("review_id") if isinstance(result, dict) else None
        logger.info(
            "orchestrator.%s completed in %.1fms (project=%s, review_id=%s)",
            name, duration_ms, project_name, review_id,
        )
        return result
    return _wrapper


class SDLCOrchestrator:
    def __init__(self):
        self.req_agent = RequirementReviewAgent()
        self.code_agent = CodeReviewAgent()
        self.prompt_agent = PromptSafetyAgent()
        self.compliance_agent = ComplianceMapperAgent()
        self.gate_agent = ReleaseGateAgent()
        self.report_agent = ReportGeneratorAgent()
        self.vapt_agent = VAPTAgent()
        self.security_agent = SecurityReviewAgent()
        self.arch_agent = ArchitectureAgent()
        self.sensitive_agent = SensitiveDataAgent()
        self.codegen_agent = CodeGenAgent()
        self.ai_provenance_agent = AICodeProvenanceAgent()
        self.migration_agent = MigrationAgent()
        self.autofix_agent = AutoFixAgent()
        self.cfr21_agent = CFR21VerificationAgent()
        self.gxp_document_agent = GxPDocumentAgent()
        self.gxp_generator_agent = GxPGeneratorAgent()
        self.issue_triage_agent = IssueTriageAgent()
        self.test_scenario_agent = TestScenarioAgent()
        self.test_case_agent = TestCaseAgent()
        self.github = GitHubService()

    @staticmethod
    def _kb(category: str, context: str, query: str, project_name: str) -> str:
        """Fold this agent's Knowledge Base docs (global + project-specific) into `context`."""
        kb_context = knowledge_base.get_context(category, query=query, project_name=project_name)
        if not kb_context:
            return context
        return f"{context}\n\n{kb_context}" if context else kb_context

    @staticmethod
    def _compress(content: str) -> tuple:
        """Cost-optimization (Headroom) — compress a raw artifact in isolation, BEFORE
        it gets wrapped into an agent's instruction prompt. Headroom's content router
        only applies real compression to messages that are purely JSON/code/log content;
        once mixed with instruction text it reads as prose and no-ops, so this must run
        on the isolated field. Returns (possibly-compressed content, cost_stats or None).
        Not called at all for regulated-evidence agents (21 CFR Part 11, GxP) — those
        pass the original content straight through untouched.
        """
        compressed, stats = cost_optimization_service.compress_content(content, config.MODEL)
        return compressed, (stats if stats.get("compressed") else None)

    @_log_call
    def review_requirement(self, content: str, context: str = "", project_name: str = "") -> Dict[str, Any]:
        context = self._kb("requirement", context, content, project_name)
        compressed_content, cost_stats = self._compress(content)
        result = self.req_agent.review_requirement(compressed_content, context)
        if cost_stats:
            result["_cost_stats"] = cost_stats
        review_id = database.save_review(
            review_type="requirement",
            project_name=project_name,
            input_summary=content[:200] + "..." if len(content) > 200 else content,
            input_content=content,
            result=result,
        )
        result["review_id"] = review_id
        return result

    @_log_call
    def review_code(self, code: str = "", language: str = "", context: str = "",
                    project_name: str = "", pr_url: str = "",
                    files: Optional[List[Dict[str, Any]]] = None,
                    coverage_incomplete: bool = False, coverage_reason: str = "",
                    source: Optional[Dict[str, Any]] = None,
                    extra_findings: Optional[List[Dict[str, Any]]] = None,
                    suppression: Any = None,
                    on_progress=None) -> Dict[str, Any]:
        """Review a paste, a diff, or a whole file list (a zip or a repository).

        **Cost-optimization compression is deliberately skipped for `files`.** Headroom
        rewrites the artifact, and on the single-artifact path that is self-consistent: the
        agent verifies evidence against the same compressed text it was shown, so a quote
        still matches. Per file it is not safe. A finding's `line_start` now anchors an
        inline GitHub review comment on the customer's real file, and a line recovered from
        compressed text does not point at the same line in the original. A few percent of
        input tokens is not worth a comment landing on the wrong line.
        """
        artifact_for_kb = code or "\n".join(
            (f.get("content") or "")[:2000] for f in (files or [])[:20]
        )
        context = self._kb("code-review", context, artifact_for_kb, project_name)

        cost_stats = None
        if files:
            result = self.code_agent.review_code(
                language=language, context=context, files=files,
                coverage_incomplete=coverage_incomplete, coverage_reason=coverage_reason,
                extra_findings=extra_findings, suppression=suppression,
                on_progress=on_progress,
            )
            label = (source or {}).get("label", "")
            input_summary = f"Code review over {len(files)} file(s)" + (f" — {label}" if label else "")
            input_content = "\n".join(f.get("path", "") for f in files)[:5000]
        else:
            compressed_code, cost_stats = self._compress(code)
            result = self.code_agent.review_code(
                code=compressed_code, language=language, context=context,
                coverage_incomplete=coverage_incomplete, coverage_reason=coverage_reason,
                suppression=suppression, on_progress=on_progress,
            )
            input_summary = f"Code review{f' for PR: {pr_url}' if pr_url else ''} ({len(code)} chars)"
            input_content = code[:5000]

        if cost_stats:
            result["_cost_stats"] = cost_stats
        if source:
            result["source"] = source
        review_id = database.save_review(
            review_type="code",
            project_name=project_name,
            input_summary=input_summary,
            input_content=input_content,
            result=result,
        )
        result["review_id"] = review_id
        return result

    @_log_call
    def review_prompt(
        self,
        system_prompt: str = "",
        user_prompt: str = "",
        tool_definitions: str = "",
        rag_config: str = "",
        context: str = "",
        project_name: str = "",
    ) -> Dict[str, Any]:
        context = self._kb("prompt-safety", context, f"{system_prompt} {user_prompt} {tool_definitions} {rag_config}", project_name)
        result = self.prompt_agent.review_prompt(
            system_prompt, user_prompt, tool_definitions, rag_config, context
        )
        review_id = database.save_review(
            review_type="prompt",
            project_name=project_name,
            input_summary="Prompt & Agent Safety Review",
            input_content=system_prompt[:2000] if system_prompt else user_prompt[:2000],
            result=result,
        )
        result["review_id"] = review_id
        return result

    @_log_call
    def review_compliance(self, content: str, frameworks: list = None, context: str = "", project_name: str = "") -> Dict[str, Any]:
        context = self._kb("compliance", context, content, project_name)
        compressed_content, cost_stats = self._compress(content)
        result = self.compliance_agent.map_compliance(compressed_content, frameworks, context)
        if cost_stats:
            result["_cost_stats"] = cost_stats
        review_id = database.save_review(
            review_type="compliance",
            project_name=project_name,
            input_summary=content[:200] + "..." if len(content) > 200 else content,
            input_content=content,
            result=result,
        )
        result["review_id"] = review_id
        return result

    @_log_call
    def full_review(
        self,
        requirement: Optional[str] = None,
        compliance_artifact: Optional[str] = None,
        architecture: Optional[str] = None,
        code: Optional[str] = None,
        prompt_config: Optional[Dict[str, str]] = None,
        test_evidence: Optional[str] = None,
        project_name: str = "Unnamed Project",
        context: str = "",
        on_progress: Optional[Any] = None,
        include_security: bool = False,
        include_vapt: bool = False,
        include_sensitive_data: bool = False,
    ) -> Dict[str, Any]:
        """`on_progress`, if given, is called as `on_progress(step_index, total_steps,
        step_label)` immediately before each sequential sub-agent call — used by the
        job-status endpoint (see job_service.py). See backend/workflows/full_review.py.

        The three `include_*` flags add opt-in stages over artifacts already supplied and
        default off, so an existing caller's run is unchanged."""
        return full_review_workflow.run(
            self,
            requirement=requirement,
            compliance_artifact=compliance_artifact,
            architecture=architecture,
            code=code,
            prompt_config=prompt_config,
            test_evidence=test_evidence,
            project_name=project_name,
            context=context,
            on_progress=on_progress,
            include_security=include_security,
            include_vapt=include_vapt,
            include_sensitive_data=include_sensitive_data,
        )
    @_log_call
    def review_vapt(
        self,
        artifact: str = "",
        scope: str = "",
        context: str = "",
        project_name: str = "Unnamed Project",
        files: Optional[List[Dict[str, Any]]] = None,
        coverage_incomplete: bool = False,
        coverage_reason: str = "",
        source: Optional[Dict[str, Any]] = None,
        extra_findings: Optional[List[Dict[str, Any]]] = None,
        suppression: Any = None,
        on_progress=None,
    ) -> Dict[str, Any]:
        """Assess a paste, a diff, or a whole file list, and persist a gated result.

        The empty-assessment refusal that used to live here is now
        `finding_gate.looks_degenerate`, applied inside `chunked_review.run` — so it covers
        every caller of the agent rather than only this one (`swarm_service` reaches the
        agent directly). The counts likewise moved onto the agent.

        Compression is skipped for `files` for the same reason as `review_code`: a line
        number recovered from compressed text does not point at the same line in the
        customer's file.
        """
        artifact_for_kb = artifact or "\n".join(
            (f.get("content") or "")[:2000] for f in (files or [])[:20]
        )
        context = self._kb("vapt", context, artifact_for_kb, project_name)

        cost_stats = None
        if files:
            result = self.vapt_agent.run_vapt(
                context=context, scope=scope, files=files,
                coverage_incomplete=coverage_incomplete, coverage_reason=coverage_reason,
                extra_findings=extra_findings, suppression=suppression,
                on_progress=on_progress,
            )
            label = (source or {}).get("label", "")
            input_summary = f"VAPT over {len(files)} file(s)" + (f" — {label}" if label else "")
            input_content = "\n".join(f.get("path", "") for f in files)[:5000]
        else:
            compressed_artifact, cost_stats = self._compress(artifact)
            result = self.vapt_agent.run_vapt(
                artifact=compressed_artifact, context=context, scope=scope,
                coverage_incomplete=coverage_incomplete, coverage_reason=coverage_reason,
                suppression=suppression, on_progress=on_progress,
            )
            input_summary = f"VAPT — {artifact[:150]}{'...' if len(artifact) > 150 else ''}"
            input_content = artifact[:5000]

        if cost_stats:
            result["_cost_stats"] = cost_stats
        if source:
            result["source"] = source
        review_id = database.save_review(
            review_type="vapt",
            project_name=project_name,
            input_summary=input_summary,
            input_content=input_content,
            result=result,
        )
        result["review_id"] = review_id
        return result

    @_log_call
    def review_security(
        self,
        artifact: str,
        system_type: str = "",
        context: str = "",
        project_name: str = "Unnamed Project",
    ) -> Dict[str, Any]:
        context = self._kb("security", context, artifact, project_name)
        compressed_artifact, cost_stats = self._compress(artifact)
        result = self.security_agent.run_security_review(compressed_artifact, context, system_type)
        if cost_stats:
            result["_cost_stats"] = cost_stats
        self._fallback_gate_from_specialized(
            result, [t.get("risk_level", "Low") for t in result.get("stride_threats", [])]
        )
        findings = result.get("findings", [])
        review_id = database.save_review(
            review_type="security",
            project_name=project_name,
            input_summary=f"Security Review — {artifact[:150]}{'...' if len(artifact) > 150 else ''}",
            input_content=artifact[:5000],
            result=result,
        )
        result["review_id"] = review_id
        result["critical_count"] = sum(1 for f in findings if f.get("severity") == 5)
        result["high_count"] = sum(1 for f in findings if f.get("severity") == 4)
        result["stride_count"] = len(result.get("stride_threats", []))
        result["boundary_count"] = len(result.get("trust_boundaries", []))
        return result

    @_log_call
    def review_architecture(
        self,
        artifact: str,
        requirements: str = "",
        review_mode: str = "review",
        context: str = "",
        project_name: str = "Unnamed Project",
    ) -> Dict[str, Any]:
        compressed_artifact, cost_stats = self._compress(artifact)
        result = self.arch_agent.run_architecture_review(compressed_artifact, context, requirements, review_mode, project_name)
        if cost_stats:
            result["_cost_stats"] = cost_stats
        anti_pattern_severities = [p.get("severity", "Low") for p in result.get("anti_patterns", [])]
        if result.get("single_points_of_failure"):
            anti_pattern_severities.append("Critical")
        self._fallback_gate_from_specialized(result, anti_pattern_severities)
        findings = result.get("findings", [])
        review_id = database.save_review(
            review_type="architecture",
            project_name=project_name,
            input_summary=f"Architecture Review [{review_mode}] — {artifact[:150]}{'...' if len(artifact) > 150 else ''}",
            input_content=artifact[:5000],
            result=result,
        )
        result["review_id"] = review_id
        result["critical_count"] = sum(1 for f in findings if f.get("severity") == 5)
        result["high_count"] = sum(1 for f in findings if f.get("severity") == 4)
        result["anti_pattern_count"] = len(result.get("anti_patterns", []))
        result["spof_count"] = len(result.get("single_points_of_failure", []))
        result["alternative_count"] = len(result.get("alternative_recommendations", []))
        return result

    @_log_call
    def scan_sensitive_data(
        self,
        content: str = "",
        context: str = "",
        project_name: str = "Unnamed Project",
        files: Optional[List[Dict[str, Any]]] = None,
        coverage_incomplete: bool = False,
        coverage_reason: str = "",
        extra_findings: Optional[List[Dict[str, Any]]] = None,
        suppression: Any = None,
        source: Optional[Dict[str, Any]] = None,
        on_progress=None,
    ) -> Dict[str, Any]:
        """One artifact, or a whole codebase fanned out over chunks.

        The file path skips Headroom compression deliberately: `chunked_review` already
        splits to fit, and compressing a codebase before splitting it would change the text
        the per-file evidence is then reconciled against.
        """
        kb_seed = content or "\n".join((f.get("path") or "") for f in (files or []))
        context = self._kb("sensitive-data", context, kb_seed, project_name)
        cost_stats = None
        if files:
            result = self.sensitive_agent.scan(
                context=context, project_name=project_name, files=files,
                coverage_incomplete=coverage_incomplete, coverage_reason=coverage_reason,
                extra_findings=extra_findings, suppression=suppression,
                source=source, on_progress=on_progress)
            summary = f"Sensitive Data Scan — {len(files)} file(s)"
            stored_input = "\n".join((f.get("path") or "") for f in files)[:5000]
        else:
            compressed_content, cost_stats = self._compress(content)
            result = self.sensitive_agent.scan(compressed_content, context, project_name)
            summary = f"Sensitive Data Scan — {len(content)} chars"
            stored_input = content[:5000]
        if cost_stats:
            result["_cost_stats"] = cost_stats
        findings = result.get("findings", [])
        review_id = database.save_review(
            review_type="sensitive_data",
            project_name=project_name,
            input_summary=summary,
            input_content=stored_input,
            result=result,
        )
        result["review_id"] = review_id
        result["total_findings"] = len(findings)
        result["critical_count"] = sum(1 for f in findings if f.get("severity") == 5)
        result["high_count"] = sum(1 for f in findings if f.get("severity") == 4)
        return result

    @_log_call
    def generate_codegen_advisory(
        self,
        requirement: str,
        constraints: str = "",
        pins: Optional[Dict[str, str]] = None,
        context: str = "",
        project_name: str = "Unnamed Project",
    ) -> Dict[str, Any]:
        result = self.codegen_agent.recommend_stack(requirement, constraints, pins, context, project_name)
        review_id = database.save_review(
            review_type="codegen_advisory",
            project_name=project_name,
            input_summary=f"Codegen advisory — {requirement[:150]}{'...' if len(requirement) > 150 else ''}",
            input_content=requirement[:5000],
            result=result,
        )
        result["review_id"] = review_id
        return result

    @_log_call
    def generate_codegen_scaffold(
        self,
        project_name: str,
        language: str,
        backend_framework: str,
        frontend_framework: str = "",
        database_choice: str = "",
        architecture_pattern: str = "Modular Monolith",
        requirement: str = "",
        include_auth: bool = True,
        include_docker: bool = True,
        include_tests: bool = True,
        auto_review: bool = False,
        context: str = "",
    ) -> Dict[str, Any]:
        return codegen_scaffold_workflow.run(
            self,
            project_name=project_name,
            language=language,
            backend_framework=backend_framework,
            frontend_framework=frontend_framework,
            database_choice=database_choice,
            architecture_pattern=architecture_pattern,
            requirement=requirement,
            include_auth=include_auth,
            include_docker=include_docker,
            include_tests=include_tests,
            auto_review=auto_review,
            context=context,
        )
    @_log_call
    def analyze_ai_provenance(
        self,
        artifact: str,
        disclosure: Optional[Dict[str, Any]] = None,
        context: str = "",
        project_name: str = "Unnamed Project",
    ) -> Dict[str, Any]:
        context = self._kb("ai-provenance", context, artifact, project_name)
        compressed_artifact, cost_stats = self._compress(artifact)
        result = self.ai_provenance_agent.analyze(compressed_artifact, disclosure, context)
        if cost_stats:
            result["_cost_stats"] = cost_stats
        review_id = database.save_review(
            review_type="ai_provenance",
            project_name=project_name,
            input_summary=f"AI provenance scan — likelihood {result.get('ai_origin_likelihood', 'Unknown')} "
                          f"({result.get('score', 0)}/100)",
            input_content=artifact[:5000],
            result=result,
        )
        result["review_id"] = review_id
        return result

    @_log_call
    def assess_migration(
        self,
        source: str,
        migration_target: str,
        constraints: str = "",
        context: str = "",
        project_name: str = "Unnamed Project",
    ) -> Dict[str, Any]:
        compressed_source, cost_stats = self._compress(source)
        result = self.migration_agent.assess(compressed_source, migration_target, constraints, context, project_name)
        if cost_stats:
            result["_cost_stats"] = cost_stats
        review_id = database.save_review(
            review_type="migration_assessment",
            project_name=project_name,
            input_summary=f"Migration assessment — {result.get('source_stack','?')} -> {result.get('target_stack', migration_target)} "
                          f"({result.get('complexity', 'Unknown')} complexity)",
            input_content=source[:5000],
            result=result,
        )
        result["review_id"] = review_id
        return result

    @_log_call
    def execute_migration(
        self,
        source: str,
        migration_target: str,
        file_path: str = "",
        requirement: str = "",
        auto_review: bool = False,
        context: str = "",
        project_name: str = "Unnamed Project",
    ) -> Dict[str, Any]:
        return migration_execute_workflow.run(
            self,
            source=source,
            migration_target=migration_target,
            file_path=file_path,
            requirement=requirement,
            auto_review=auto_review,
            context=context,
            project_name=project_name,
        )
    @_log_call
    def verify_cfr21(
        self,
        files: Optional[List[Dict[str, str]]] = None,
        code: str = "",
        system_description: str = "",
        context: str = "",
        project_name: str = "Unnamed Project",
    ) -> Dict[str, Any]:
        files = files or []
        result = self.cfr21_agent.verify(files, code, system_description, context, project_name)
        findings = result.get("findings", [])
        input_content = "\n\n".join(f.get("content", "") for f in files) or code
        review_id = database.save_review(
            review_type="cfr21_verification",
            project_name=project_name,
            input_summary=f"21 CFR Part 11 verification — {len(files) or 1} file(s), "
                          f"gate={result.get('overall_gate_status', 'Unknown')}",
            input_content=input_content[:5000],
            result={**result, "findings": findings, "risk_score": result.get("overall_risk_score", 0),
                    "gate_status": result.get("overall_gate_status", "Green")},
        )
        result["review_id"] = review_id
        result["critical_count"] = sum(1 for f in findings if f.get("severity") == 5)
        result["high_count"] = sum(1 for f in findings if f.get("severity") == 4)
        result["total_findings"] = len(findings)
        return result

    @_log_call
    def check_gxp_documents(
        self,
        documents: Optional[List[Dict[str, str]]] = None,
        content: str = "",
        document_type_hint: str = "",
        context: str = "",
        project_name: str = "Unnamed Project",
    ) -> Dict[str, Any]:
        documents = documents or []
        result = self.gxp_document_agent.check(documents, content, document_type_hint, context, project_name)
        findings = result.get("findings", [])
        input_content = "\n\n".join(d.get("content", "") for d in documents) or content
        review_id = database.save_review(
            review_type="gxp_document_check",
            project_name=project_name,
            input_summary=f"GxP document check — {result.get('document_type', 'Unknown')} "
                          f"({result.get('completeness_score', 0)}% complete, gate={result.get('overall_gate_status', 'Unknown')})",
            input_content=input_content[:5000],
            result={**result, "findings": findings, "risk_score": result.get("overall_risk_score", 0),
                    "gate_status": result.get("overall_gate_status", "Green")},
        )
        result["review_id"] = review_id
        result["critical_count"] = sum(1 for f in findings if f.get("severity") == 5)
        result["high_count"] = sum(1 for f in findings if f.get("severity") == 4)
        result["total_findings"] = len(findings)
        return result

    @_log_call
    def plan_gxp_validation_package(
        self,
        files: Optional[List[Dict[str, str]]] = None,
        code: str = "",
        system_description: str = "",
        context: str = "",
        project_name: str = "Unnamed Project",
    ) -> Dict[str, Any]:
        files = files or []
        result = self.gxp_generator_agent.plan(files, code, system_description, context, project_name)
        input_content = "\n\n".join(f.get("content", "") for f in files) or code
        review_id = database.save_review(
            review_type="gxp_validation_plan",
            project_name=project_name,
            input_summary=f"GxP validation plan — {result.get('gxp_classification', 'Unknown')}, "
                          f"{sum(1 for d in result.get('applicable_documents', []) if d.get('applicable'))} documents applicable",
            input_content=input_content[:5000],
            result=result,
        )
        result["review_id"] = review_id
        return result

    @_log_call
    def generate_gxp_document(
        self,
        document_type: str,
        files: Optional[List[Dict[str, str]]] = None,
        code: str = "",
        system_description: str = "",
        outline: Optional[List[str]] = None,
        plan_context: str = "",
        context: str = "",
        project_name: str = "Unnamed Project",
    ) -> Dict[str, Any]:
        files = files or []
        result = self.gxp_generator_agent.generate_document(
            document_type, files, code, system_description, outline, plan_context, context, project_name,
        )
        input_content = "\n\n".join(f.get("content", "") for f in files) or code
        review_id = database.save_review(
            review_type="gxp_document_generated",
            project_name=project_name,
            input_summary=f"GxP document generated — {result.get('document_type', document_type)}",
            input_content=input_content[:5000],
            result=result,
        )
        result["review_id"] = review_id
        return result

    # ── Issue Triaging ────────────────────────────────────────────────────

    @_log_call
    def triage_issue(
        self,
        title: str = "", description: str = "", logs: str = "", stack_trace: str = "",
        error_code: str = "", environment: str = "", application: str = "", module: str = "",
        reporter: str = "", build_version: str = "", test_case_ref: str = "",
        customer_impact_notes: str = "", team_roster: str = "", context: str = "",
        project_name: str = "Unnamed Project", llm_override: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        return issue_triage_workflow.run(
            self,
            title=title, description=description, logs=logs, stack_trace=stack_trace,
            error_code=error_code, environment=environment, application=application,
            module=module, reporter=reporter, build_version=build_version,
            test_case_ref=test_case_ref, customer_impact_notes=customer_impact_notes,
            team_roster=team_roster, context=context, project_name=project_name,
            llm_override=llm_override,
        )
    @_log_call
    def triage_github_repo_issues(
        self,
        github_repo_url: str,
        limit: int = 10,
        team_roster: str = "",
        context: str = "",
        project_name: str = "Unnamed Project",
        llm_override: Optional[Dict[str, Any]] = None,
        on_progress: Optional[Any] = None,
    ) -> Dict[str, Any]:
        return issue_triage_batch_workflow.run(
            self,
            github_repo_url=github_repo_url,
            limit=limit,
            team_roster=team_roster,
            context=context,
            project_name=project_name,
            llm_override=llm_override,
            on_progress=on_progress,
        )
    @_log_call
    def generate_test_scenarios(
        self,
        requirement: str,
        context: str = "",
        project_name: str = "Unnamed Project",
    ) -> Dict[str, Any]:
        context = self._kb("qa", context, requirement, project_name)
        compressed_requirement, cost_stats = self._compress(requirement)
        result = self.test_scenario_agent.generate(compressed_requirement, context)
        if cost_stats:
            result["_cost_stats"] = cost_stats
        scenarios = result.get("scenarios", [])
        review_id = database.save_review(
            review_type="test_scenarios",
            project_name=project_name,
            input_summary=f"Test scenarios — {len(scenarios)} generated from: {requirement[:120]}{'...' if len(requirement) > 120 else ''}",
            input_content=requirement[:5000],
            result=result,
        )
        result["review_id"] = review_id
        result["total_scenarios"] = len(scenarios)
        result["critical_count"] = sum(1 for s in scenarios if s.get("priority") == "Critical")
        return result

    @_log_call
    def generate_test_cases(
        self,
        requirement: str,
        scenarios_context: str = "",
        context: str = "",
        project_name: str = "Unnamed Project",
    ) -> Dict[str, Any]:
        context = self._kb("qa", context, requirement, project_name)
        compressed_requirement, cost_stats = self._compress(requirement)
        result = self.test_case_agent.generate(compressed_requirement, scenarios_context, context)
        test_cases = result.get("test_cases", [])
        if not test_cases and not (result.get("summary") or "").strip():
            raise RuntimeError(
                "Test case generation returned no test cases (no test_cases and no summary) — "
                "this usually means the LLM response was truncated or malformed rather than "
                "a genuine empty result. Try again or reduce scope (e.g. fewer scenarios at once)."
            )
        if cost_stats:
            result["_cost_stats"] = cost_stats
        review_id = database.save_review(
            review_type="test_cases",
            project_name=project_name,
            input_summary=f"Test cases — {len(test_cases)} generated from: {requirement[:120]}{'...' if len(requirement) > 120 else ''}",
            input_content=requirement[:5000],
            result=result,
        )
        result["review_id"] = review_id
        result["total_test_cases"] = len(test_cases)
        result["critical_count"] = sum(1 for tc in test_cases if tc.get("priority") == "Critical")
        return result

    @_log_call
    def generate_standalone_report(
        self,
        project_name: str,
        review_ids: Optional[List[str]] = None,
        findings: Optional[List[Dict[str, Any]]] = None,
        gate_status: str = "",
        overall_risk_score: Optional[float] = None,
        context: str = "",
    ) -> Dict[str, Any]:
        """Synthesize prior findings into an audit-ready governance report, without
        re-running a full review. See backend/workflows/standalone_report.py."""
        return standalone_report_workflow.run(
            self,
            project_name=project_name,
            review_ids=review_ids,
            findings=findings,
            gate_status=gate_status,
            overall_risk_score=overall_risk_score,
            context=context,
        )
    @_log_call
    def run_release_gate(
        self,
        project_name: str,
        review_ids: list = None,
        release_version: str = "",
        evidence_summary: str = "",
        context: str = "",
        findings: list = None,
    ) -> Dict[str, Any]:
        # `context` is accepted for backwards compatibility with existing callers but is
        # not used — the gate agent takes its input from findings and evidence only.
        # `findings` is the pasted-findings path the legacy `/api/release-gate` route
        # needs; it used to evaluate those inline and persist nothing.
        return release_gate_workflow.run(
            self,
            project_name=project_name,
            review_ids=review_ids,
            release_version=release_version,
            evidence_summary=evidence_summary,
            findings=findings,
        )
    @_log_call
    def review_github_pr(self, pr_url: str, jira_ticket: str = "",
                         include_prompt_review: bool = False,
                         post_mode: str = "summary", set_status: bool = True,
                         request_changes: bool = False, on_progress=None) -> Dict[str, Any]:
        # `include_prompt_review` has never been implemented; kept so existing callers
        # that pass it keep working.
        return review_pr_workflow.run(
            self, pr_url=pr_url, jira_ticket=jira_ticket, post_mode=post_mode,
            set_status=set_status, request_changes=request_changes, on_progress=on_progress,
        )
    @_log_call
    def auto_fix_github_pr(self, pr_url: str, jira_ticket: str = "", max_files: int = 5) -> Dict[str, Any]:
        """Review a GitHub PR, then commit fixes for its findings to a new branch off the PR's head branch
        and open a follow-up PR (head_branch -> lm-autofix/pr-N -> new PR back into head_branch)."""
        return autofix_pr_workflow.run(self, pr_url=pr_url, jira_ticket=jira_ticket, max_files=max_files)

    @_log_call
    def modernization_pr(self, run_id: str, owner: str, repo: str, callback_url: str,
                         base_branch: str = "", dotnet_version: str = "8.0.x",
                         on_progress=None) -> Dict[str, Any]:
        """Push a modernization run's converted codebase to a branch and open a PR carrying a
        build workflow that reports its diagnostics back to `callback_url`.

        This is how the fix loop reaches a compiler without one running here (D-05, D-62):
        the customer's CI builds, posts to `POST /runs/{id}/build-results`, and failing units
        return to the conversion queue."""
        return modernization_pr_workflow.run(
            self, run_id=run_id, owner=owner, repo=repo, callback_url=callback_url,
            base_branch=base_branch, dotnet_version=dotnet_version, on_progress=on_progress)
    @staticmethod
    def _fallback_gate_from_specialized(result: Dict[str, Any], specialized_severities: List[str]) -> None:
        """Safety net for security/architecture reviews: their tool schemas put
        risk_score/gate_status after large specialized arrays (stride_threats,
        anti_patterns, single_points_of_failure), so a response truncated by the
        token budget can populate real specialized findings while leaving
        gate_status/risk_score at their untouched schema default (Green/0). If
        that exact signature shows up — default Green, no generic findings, but
        real specialized data — recompute the gate deterministically from the
        specialized severities instead of trusting the (likely truncated) default.
        A genuinely clean review (nothing found anywhere) is left untouched.
        """
        if result.get("gate_status", "Green") != "Green" or result.get("findings") or not specialized_severities:
            return
        if "Critical" in specialized_severities:
            result["gate_status"], result["risk_score"] = "Red", 95.0
        elif "High" in specialized_severities:
            result["gate_status"], result["risk_score"] = "Amber", 70.0
        else:
            result["gate_status"], result["risk_score"] = "Amber", 40.0

    def _calculate_overall_risk(self, results: List[Dict[str, Any]]) -> float:
        return workflow_shared.calculate_overall_risk(results)
