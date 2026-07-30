import functools
import json
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
from backend.services import guardrail_service
from backend import config, database

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
    def review_code(self, code: str, language: str = "", context: str = "", project_name: str = "", pr_url: str = "") -> Dict[str, Any]:
        context = self._kb("code-review", context, code, project_name)
        compressed_code, cost_stats = self._compress(code)
        result = self.code_agent.review_code(compressed_code, language, context)
        if cost_stats:
            result["_cost_stats"] = cost_stats
        review_id = database.save_review(
            review_type="code",
            project_name=project_name,
            input_summary=f"Code review{f' for PR: {pr_url}' if pr_url else ''} ({len(code)} chars)",
            input_content=code[:5000],
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
    ) -> Dict[str, Any]:
        review_results: List[Dict[str, Any]] = []
        all_findings: List[Dict[str, Any]] = []

        if requirement:
            req_result = self.req_agent.review_requirement(requirement, context)
            req_result["source"] = "requirement"
            review_results.append(req_result)
            all_findings.extend(req_result.get("findings", []))

        if compliance_artifact:
            compliance_context = self._kb("compliance", context, compliance_artifact, project_name)
            compliance_result = self.compliance_agent.map_compliance(
                compliance_artifact, context=compliance_context
            )
            compliance_result["source"] = "compliance"
            review_results.append(compliance_result)
            all_findings.extend(compliance_result.get("findings", []))

        if architecture:
            arch_result = self.arch_agent.run_architecture_review(architecture, context, project_name=project_name)
            arch_result["source"] = "architecture"
            review_results.append(arch_result)
            all_findings.extend(arch_result.get("findings", []))

        if code:
            code_result = self.code_agent.review_code(code, context=context)
            code_result["source"] = "code"
            review_results.append(code_result)
            all_findings.extend(code_result.get("findings", []))

        if prompt_config:
            prompt_result = self.prompt_agent.review_prompt(
                system_prompt=prompt_config.get("system_prompt", ""),
                user_prompt=prompt_config.get("user_prompt", ""),
                tool_definitions=prompt_config.get("tool_definitions", ""),
                rag_config=prompt_config.get("rag_config", ""),
                context=context,
            )
            prompt_result["source"] = "prompt"
            review_results.append(prompt_result)
            all_findings.extend(prompt_result.get("findings", []))

        overall_risk = self._calculate_overall_risk(review_results)
        gate_result = self.gate_agent.evaluate_gate(
            all_findings=all_findings,
            project_name=project_name,
            evidence_summary=test_evidence or "",
        )
        gate_result["source"] = "release_gate"
        review_results.append(gate_result)

        final_gate = gate_result.get("gate_status", "Red")

        report = self.report_agent.generate_report(
            project_name=project_name,
            all_findings=all_findings,
            review_results=review_results,
            gate_status=final_gate,
            overall_risk_score=overall_risk,
            context=context,
        )

        report_id = database.save_report(
            review_id=None,
            project_name=project_name,
            report=report,
        )

        review_id = database.save_review(
            review_type="full",
            project_name=project_name,
            input_summary=f"Full SDLC review — {len(review_results)} sub-reviews, {len(all_findings)} findings",
            input_content=requirement or compliance_artifact or architecture or code or "",
            result={
                "findings": all_findings,
                "risk_score": overall_risk,
                "gate_status": final_gate,
                "summary": gate_result.get("summary", ""),
                "compliance_flags": list({
                    flag
                    for r in review_results
                    for flag in r.get("compliance_flags", [])
                }),
                "recommendations": gate_result.get("recommendations", []),
                "review_results": review_results,
                "report_id": report_id,
            },
        )

        return {
            "review_id": review_id,
            "report_id": report_id,
            "project_name": project_name,
            "gate_status": final_gate,
            "overall_risk_score": overall_risk,
            "total_findings": len(all_findings),
            "critical_count": sum(1 for f in all_findings if f.get("severity") == 5),
            "high_count": sum(1 for f in all_findings if f.get("severity") == 4),
            "sub_reviews": [
                {
                    "source": r.get("source", "unknown"),
                    "agent": r.get("agent_name", ""),
                    "gate": r.get("gate_status", ""),
                    "score": r.get("risk_score", 0),
                    "findings": len(r.get("findings", [])),
                }
                for r in review_results
            ],
            "summary": gate_result.get("summary", ""),
            "top_findings": sorted(all_findings, key=lambda f: f.get("severity", 0), reverse=True)[:10],
            "recommendations": gate_result.get("recommendations", []),
        }

    @_log_call
    def review_vapt(
        self,
        artifact: str,
        scope: str = "",
        context: str = "",
        project_name: str = "Unnamed Project",
    ) -> Dict[str, Any]:
        context = self._kb("vapt", context, artifact, project_name)
        compressed_artifact, cost_stats = self._compress(artifact)
        result = self.vapt_agent.run_vapt(compressed_artifact, context, scope)
        findings = result.get("findings", [])
        if not findings and not (result.get("summary") or "").strip():
            raise RuntimeError(
                "VAPT agent returned an empty assessment (no findings and no summary) — "
                "this usually means the LLM response was truncated or malformed rather than "
                "a genuine clean result. Try again or narrow the artifact/scope."
            )
        if cost_stats:
            result["_cost_stats"] = cost_stats
        review_id = database.save_review(
            review_type="vapt",
            project_name=project_name,
            input_summary=f"VAPT — {artifact[:150]}{'...' if len(artifact) > 150 else ''}",
            input_content=artifact[:5000],
            result=result,
        )
        result["review_id"] = review_id
        result["critical_count"] = sum(1 for f in findings if f.get("severity") == 5)
        result["high_count"] = sum(1 for f in findings if f.get("severity") == 4)
        result["total_findings"] = len(findings)
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
        content: str,
        context: str = "",
        project_name: str = "Unnamed Project",
    ) -> Dict[str, Any]:
        context = self._kb("sensitive-data", context, content, project_name)
        compressed_content, cost_stats = self._compress(content)
        result = self.sensitive_agent.scan(compressed_content, context, project_name)
        if cost_stats:
            result["_cost_stats"] = cost_stats
        findings = result.get("findings", [])
        review_id = database.save_review(
            review_type="sensitive_data",
            project_name=project_name,
            input_summary=f"Sensitive Data Scan — {len(content)} chars",
            input_content=content[:5000],
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
        result = self.codegen_agent.generate_scaffold(
            project_name=project_name,
            language=language,
            backend_framework=backend_framework,
            frontend_framework=frontend_framework,
            database=database_choice,
            architecture_pattern=architecture_pattern,
            requirement=requirement,
            include_auth=include_auth,
            include_docker=include_docker,
            include_tests=include_tests,
            context=context,
        )

        files = result.get("files", [])
        if not files:
            raise RuntimeError(
                "Scaffold generation returned no files — this usually means the LLM response "
                "was truncated or malformed rather than a deliberate empty scaffold. Try again "
                "with a narrower scope (fewer options, e.g. skip docker/tests) or retry."
            )
        if auto_review and files:
            combined = "\n\n".join(f"# ── {f.get('path', '')} ──\n{f.get('content', '')}" for f in files)
            code_review = self.code_agent.review_code(combined, language, "Generated scaffold — pre-release governance check")
            sensitive_scan = self.sensitive_agent.scan(combined, "Generated scaffold", project_name)
            result["governance_review"] = {
                "code_review": {
                    "gate_status": code_review.get("gate_status", "Green"),
                    "risk_score": code_review.get("risk_score", 0),
                    "findings": code_review.get("findings", []),
                    "summary": code_review.get("summary", ""),
                },
                "sensitive_data_scan": {
                    "gate_status": sensitive_scan.get("gate_status", "Green"),
                    "risk_score": sensitive_scan.get("risk_score", 0),
                    "findings": sensitive_scan.get("findings", []),
                    "summary": sensitive_scan.get("summary", ""),
                },
            }

        review_id = database.save_review(
            review_type="codegen_scaffold",
            project_name=project_name,
            input_summary=f"Codegen scaffold — {language}/{backend_framework}"
                          f"{'/' + frontend_framework if frontend_framework else ''} ({len(files)} files)",
            input_content=requirement[:5000],
            result=result,
        )
        result["review_id"] = review_id
        return result

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
        # Not compressed: this generates replacement code 1:1 from `source` — exact
        # source fidelity matters more here than for a review-only agent.
        result = self.migration_agent.execute(source, migration_target, file_path, requirement, context, project_name)

        files = result.get("files", [])
        if auto_review and files:
            combined = "\n\n".join(f"# ── {f.get('path', '')} ──\n{f.get('content', '')}" for f in files)
            code_review = self.code_agent.review_code(combined, "", "Migrated code — pre-release governance check")
            sensitive_scan = self.sensitive_agent.scan(combined, "Migrated code", project_name)
            result["governance_review"] = {
                "code_review": {
                    "gate_status": code_review.get("gate_status", "Green"),
                    "risk_score": code_review.get("risk_score", 0),
                    "findings": code_review.get("findings", []),
                    "summary": code_review.get("summary", ""),
                },
                "sensitive_data_scan": {
                    "gate_status": sensitive_scan.get("gate_status", "Green"),
                    "risk_score": sensitive_scan.get("risk_score", 0),
                    "findings": sensitive_scan.get("findings", []),
                    "summary": sensitive_scan.get("summary", ""),
                },
            }

        review_id = database.save_review(
            review_type="migration_execution",
            project_name=project_name,
            input_summary=f"Migration execution — {migration_target} ({len(files)} files)",
            input_content=source[:5000],
            result=result,
        )
        result["review_id"] = review_id
        return result

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

    @staticmethod
    def _build_issue_content(
        title: str = "", description: str = "", logs: str = "", stack_trace: str = "",
        error_code: str = "", environment: str = "", application: str = "", module: str = "",
        reporter: str = "", build_version: str = "", test_case_ref: str = "",
        customer_impact_notes: str = "",
    ) -> str:
        parts = [f"**Title:** {title}" if title else "**Title:** Unavailable"]
        parts.append(f"**Description:** {description}" if description else "**Description:** Unavailable")
        if application:
            parts.append(f"**Application:** {application}")
        if module:
            parts.append(f"**Module:** {module}")
        if environment:
            parts.append(f"**Environment:** {environment}")
        if build_version:
            parts.append(f"**Build/Release Version:** {build_version}")
        if reporter:
            parts.append(f"**Reporter:** {reporter}")
        if error_code:
            parts.append(f"**Error Code:** {error_code}")
        if test_case_ref:
            parts.append(f"**Related Test Case / Requirement:** {test_case_ref}")
        if customer_impact_notes:
            parts.append(f"**Customer Impact Notes:** {customer_impact_notes}")
        if stack_trace:
            parts.append(f"**Stack Trace:**\n```\n{stack_trace[:4000]}\n```")
        if logs:
            parts.append(f"**Logs:**\n```\n{logs[:4000]}\n```")
        return "\n\n".join(parts)

    @staticmethod
    def _build_known_issues_context(project_name: str, exclude_review_id: str = "") -> str:
        prior = database.list_reviews_by_type("issue_triage", project_name, limit=25)
        lines = []
        for row in prior:
            if row["id"] == exclude_review_id:
                continue
            result = row.get("result", {})
            summary = (result.get("issue_summary") or row.get("input_summary") or "")[:150]
            lines.append(
                f"- [{row['id']}] {result.get('issue_category', '?')} | "
                f"{result.get('affected_application', '?')}/{result.get('affected_module', '?')} | "
                f"{result.get('environment', '?')} | {result.get('severity', '?')}/{result.get('priority', '?')} | {summary}"
            )
        return "\n".join(lines)

    @staticmethod
    def _compute_triage_gate(result: Dict[str, Any]) -> Dict[str, Any]:
        """Deterministic human-in-the-loop policy — never left to the model's judgment.

        >90% confidence, non-critical, non-security -> Green (auto-classify/assign)
        70-90% confidence -> Amber (recommend + request confirmation)
        <70% confidence, OR Critical severity, OR P0/P1 priority, OR security/compliance impact -> Red (mandatory human validation)
        """
        severity = result.get("severity", "Low")
        priority = result.get("priority", "P3 - Low priority")
        confidence = result.get("confidence_score", 0)
        sec_impact = result.get("security_compliance_impact", {}) or {}
        is_security_category = result.get("issue_category") == "Security vulnerability"

        mandatory_human = (
            severity == "Critical"
            or priority.startswith("P0") or priority.startswith("P1")
            or bool(sec_impact.get("has_impact"))
            or is_security_category
            or confidence < 70
        )
        if mandatory_human:
            gate_status = "Red"
        elif confidence < 90:
            gate_status = "Amber"
        else:
            gate_status = "Green"

        risk_score = {"Critical": 95, "High": 70, "Medium": 40, "Low": 15}.get(severity, 15)

        findings = []
        for item in (result.get("enrichment", {}) or {}).get("missing_information", []):
            findings.append({
                "control_id": "TRIAGE-INFO", "severity": 2, "risk_level": "Minor",
                "title": "Missing information", "description": item,
                "recommendation": "Request this information from the reporter before further investigation.",
                "is_blocking": False,
            })
        if sec_impact.get("has_impact"):
            findings.append({
                "control_id": "TRIAGE-SEC", "severity": 5, "risk_level": "Critical",
                "title": "Security/compliance impact flagged", "description": sec_impact.get("details", ""),
                "recommendation": "Route to the security team for validation before any public disclosure or fix rollout.",
                "is_blocking": True,
            })
        escalation = result.get("escalation", {}) or {}
        if escalation.get("required"):
            findings.append({
                "control_id": "TRIAGE-ESC", "severity": 4, "risk_level": "High",
                "title": "Escalation required", "description": "; ".join(escalation.get("reasons", [])) or "Escalation criteria met.",
                "recommendation": "Notify: " + (", ".join(escalation.get("notify_channels", [])) or "the responsible on-call/lead."),
                "is_blocking": True,
            })

        return {
            "gate_status": gate_status,
            "risk_score": risk_score,
            "findings": findings,
            "human_approval_required": mandatory_human,
        }

    @_log_call
    def triage_issue(
        self,
        title: str = "", description: str = "", logs: str = "", stack_trace: str = "",
        error_code: str = "", environment: str = "", application: str = "", module: str = "",
        reporter: str = "", build_version: str = "", test_case_ref: str = "",
        customer_impact_notes: str = "", team_roster: str = "", context: str = "",
        project_name: str = "Unnamed Project", llm_override: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        # Compress logs/stack_trace in ISOLATION, before they're folded into the mixed
        # prose+data issue_content blob below — Headroom's router only compresses
        # messages that are purely log/JSON/code content, not prose-wrapped mixes.
        compressed_logs, logs_stats = self._compress(logs) if logs else (logs, None)
        compressed_stack_trace, stack_stats = self._compress(stack_trace) if stack_trace else (stack_trace, None)
        cost_stats = logs_stats or stack_stats
        if logs_stats and stack_stats:
            cost_stats = {
                k: (logs_stats[k] + stack_stats[k]) if k in ("tokens_before", "tokens_after", "tokens_saved", "cost_saved_usd") else logs_stats[k]
                for k in logs_stats
            }

        issue_content = self._build_issue_content(
            title, description, compressed_logs, compressed_stack_trace, error_code, environment, application,
            module, reporter, build_version, test_case_ref, customer_impact_notes,
        )
        known_issues_context = self._build_known_issues_context(project_name)
        context = self._kb("issue-triage", context, issue_content, project_name)
        result = self.issue_triage_agent.triage(issue_content, known_issues_context, team_roster, context, llm_override)

        gate = self._compute_triage_gate(result)
        result.update(gate)
        if cost_stats:
            result["_cost_stats"] = cost_stats

        review_id = database.save_review(
            review_type="issue_triage",
            project_name=project_name,
            input_summary=f"Issue triage — {result.get('issue_category', '?')} — "
                          f"{result.get('severity', '?')}/{result.get('priority', '?')} — "
                          f"{(title or result.get('issue_summary', ''))[:80]}",
            input_content=issue_content[:5000],
            result=result,
        )
        result["review_id"] = review_id
        return result

    @_log_call
    def triage_github_repo_issues(
        self,
        github_repo_url: str,
        limit: int = 10,
        team_roster: str = "",
        context: str = "",
        project_name: str = "Unnamed Project",
        llm_override: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        issues = self.github.list_open_issues(github_repo_url, limit)
        results = []
        for issue in issues:
            r = self.triage_issue(
                title=issue.get("title", ""),
                description=issue.get("body", ""),
                context=f"GitHub issue #{issue.get('number')} — {issue.get('url', '')}\n{context}".strip(),
                team_roster=team_roster,
                project_name=project_name,
                llm_override=llm_override,
            )
            r["github_issue_number"] = issue.get("number")
            r["github_issue_url"] = issue.get("url", "")
            results.append(r)

        return {
            "repo": github_repo_url,
            "issues_fetched": len(issues),
            "issues_triaged": len(results),
            "results": results,
            "severity_breakdown": {
                s: sum(1 for r in results if r.get("severity") == s) for s in ("Critical", "High", "Medium", "Low")
            },
            "escalations_required": sum(1 for r in results if r.get("human_approval_required")),
        }

    # ── QA Intelligence ──────────────────────────────────────────────────

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
        if cost_stats:
            result["_cost_stats"] = cost_stats
        test_cases = result.get("test_cases", [])
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
        re-running a full review. Mirrors run_release_gate's review_ids aggregation."""
        all_findings: List[Dict[str, Any]] = list(findings or [])
        review_results: List[Dict[str, Any]] = []
        if review_ids:
            for rid in review_ids:
                review = database.get_review(rid)
                if review:
                    result_data = json.loads(review.get("result_json", "{}"))
                    all_findings.extend(result_data.get("findings", []))
                    review_results.append(result_data)

        if not gate_status:
            gate_status = "Red" if any(f.get("severity") == 5 for f in all_findings) else (
                "Amber" if any(f.get("severity") == 4 for f in all_findings) else "Green"
            )
        if overall_risk_score is None:
            overall_risk_score = self._calculate_overall_risk(review_results) if review_results else 0.0

        report = self.report_agent.generate_report(
            project_name=project_name,
            all_findings=all_findings,
            review_results=review_results,
            gate_status=gate_status,
            overall_risk_score=overall_risk_score,
            context=context,
        )
        report_id = database.save_report(
            review_id=None,
            project_name=project_name,
            report=report,
        )
        report["report_id"] = report_id
        return report

    @_log_call
    def run_release_gate(
        self,
        project_name: str,
        review_ids: list = None,
        release_version: str = "",
        evidence_summary: str = "",
        context: str = "",
    ) -> Dict[str, Any]:
        all_findings: list = []
        if review_ids:
            for rid in review_ids:
                review = database.get_review(rid)
                if review:
                    result_data = __import__("json").loads(review.get("result_json", "{}"))
                    all_findings.extend(result_data.get("findings", []))

        result = self.gate_agent.evaluate_gate(
            all_findings=all_findings,
            project_name=project_name,
            release_version=release_version,
            evidence_summary=evidence_summary,
        )
        findings = result.get("findings", [])
        review_id = database.save_review(
            review_type="release_gate",
            project_name=project_name,
            input_summary=f"Release Gate — {project_name} {release_version}",
            input_content=evidence_summary[:2000],
            result=result,
        )
        result["review_id"] = review_id
        result["total_findings"] = len(findings)
        result["critical_count"] = sum(1 for f in findings if f.get("severity") == 5)
        result["high_count"] = sum(1 for f in findings if f.get("severity") == 4)
        result["aggregated_reviews"] = len(review_ids) if review_ids else 0
        return result

    @_log_call
    def review_github_pr(self, pr_url: str, jira_ticket: str = "", include_prompt_review: bool = False) -> Dict[str, Any]:
        pr_data = self.github.get_pr_diff(pr_url)

        context = f"GitHub PR: {pr_data['title']}\nBranch: {pr_data['head_branch']} → {pr_data['base_branch']}"
        if jira_ticket:
            context += f"\nJira ticket: {jira_ticket}"

        code_result = self.code_agent.review_code(
            code=guardrail_service.wrap_untrusted_content(pr_data["diff"], "GITHUB_PR_DIFF"),
            language="",
            context=context,
        )

        database.save_review(
            review_type="code",
            project_name=pr_data["title"],
            input_summary=f"PR #{pr_data['pr_number']}: {pr_data['title']}",
            input_content=pr_data["diff"][:5000],
            result=code_result,
        )

        comment = self.github.format_pr_review_comment(code_result)
        posted = self.github.post_pr_comment(pr_url, comment)

        return {
            "pr_url": pr_url,
            "pr_title": pr_data["title"],
            "pr_number": pr_data["pr_number"],
            "review_result": code_result,
            "comment_posted": posted,
        }

    @_log_call
    def auto_fix_github_pr(self, pr_url: str, jira_ticket: str = "", max_files: int = 5) -> Dict[str, Any]:
        """Review a GitHub PR, then commit fixes for its findings to a new branch off the PR's head branch
        and open a follow-up PR (head_branch -> lm-autofix/pr-N -> new PR back into head_branch)."""
        pr_data = self.github.get_pr_diff(pr_url)
        owner, repo, pr_number = self.github.parse_pr_url(pr_url)

        context = f"GitHub PR: {pr_data['title']}\nBranch: {pr_data['head_branch']} -> {pr_data['base_branch']}"
        if jira_ticket:
            context += f"\nJira ticket: {jira_ticket}"

        code_result = self.code_agent.review_code(
            code=guardrail_service.wrap_untrusted_content(pr_data["diff"], "GITHUB_PR_DIFF"),
            language="", context=context,
        )

        base_result = {
            "pr_url": pr_url,
            "pr_title": pr_data["title"],
            "pr_number": pr_data["pr_number"],
            "review_result": code_result,
            "fix_applied": False,
        }

        findings = code_result.get("findings", [])
        fixable_findings = [f for f in findings if f.get("is_blocking")] or findings
        if not fixable_findings:
            return {**base_result, "reason": "No findings to fix."}

        files = []
        for path in pr_data.get("files_changed", [])[:max_files]:
            try:
                fd = self.github.get_file_content_at_ref(owner, repo, path, pr_data["head_branch"])
                files.append({"path": path, "content": fd["content"]})
            except Exception:
                continue
        if not files:
            return {**base_result, "reason": "Could not fetch content for any changed file (deleted/binary/renamed)."}

        fix_result = self.autofix_agent.generate_fix(files=files, findings=fixable_findings, context=context)
        patches = fix_result.get("patches", [])
        if not patches:
            return {**base_result, "fix_result": fix_result, "reason": "Auto-Fix Agent produced no patches for these findings."}

        # Content/data safety guardrail: never commit a patch that introduces a new hardcoded
        # secret, regardless of what finding it was meant to fix.
        safe_patches, blocked_patches = [], []
        for p in patches:
            secret_hits = guardrail_service.scan_for_hardcoded_secrets(p.get("fixed_content", ""))
            if secret_hits:
                blocked_patches.append({"path": p["path"], "reason": "Blocked: patch appears to introduce a hardcoded secret.", "matches": secret_hits})
            else:
                safe_patches.append(p)

        if not safe_patches:
            return {
                **base_result, "fix_result": fix_result, "blocked_patches": blocked_patches,
                "reason": "Every generated patch was blocked by the secret-scan guardrail — nothing committed.",
            }

        fix_branch = f"lm-autofix/pr-{pr_number}"
        self.github.create_branch(owner, repo, fix_branch, pr_data["head_branch"])

        commit_message = fix_result.get("commit_message") or f"fix: auto-remediate findings from PR #{pr_number}"
        files_patched = []
        for p in safe_patches:
            committed = self.github.commit_file(owner, repo, fix_branch, p["path"], p["fixed_content"], commit_message)
            files_patched.append({**committed, "explanation": p.get("explanation", "")})

        pr_body = (
            f"{fix_result.get('pr_description', '')}\n\n---\n"
            f"*Auto-generated by [LM AI-SDLC ReviewOps Agent](https://learnedmachine.ai) to remediate findings "
            f"from #{pr_number}.*"
        )
        new_pr = self.github.create_pull_request(
            owner, repo,
            title=fix_result.get("pr_title") or f"Auto-fix: remediate findings from PR #{pr_number}",
            body=pr_body,
            head=fix_branch,
            base=pr_data["head_branch"],
        )

        database.save_review(
            review_type="code",
            project_name=pr_data["title"],
            input_summary=f"Auto-fix for PR #{pr_number}: {pr_data['title']}",
            input_content=pr_data["diff"][:5000],
            result=code_result,
        )

        return {
            **base_result,
            "fix_applied": True,
            "fix_result": fix_result,
            "fix_branch": fix_branch,
            "files_patched": files_patched,
            "blocked_patches": blocked_patches,
            "fix_pr_url": new_pr.get("url", ""),
            "fix_pr_number": new_pr.get("number"),
        }

    def _calculate_overall_risk(self, results: List[Dict[str, Any]]) -> float:
        if not results:
            return 0.0
        scores = [r.get("risk_score", 0) for r in results if r.get("risk_score") is not None]
        if not scores:
            return 0.0
        weighted = max(scores) * 0.5 + sum(scores) / len(scores) * 0.5
        return round(min(weighted, 100.0), 1)
