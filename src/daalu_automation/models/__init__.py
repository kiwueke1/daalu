"""ORM models — importing this package registers every table on ``Base.metadata``."""

from daalu_automation.models.agent_run import AgentRun
from daalu_automation.models.aiperf_run import AiperfRun, AiperfRunState
from daalu_automation.models.alert import (
    Alert,
    AlertOccurrence,
    AlertSeverity,
    AlertStatus,
)
from daalu_automation.models.alert_chat import (
    ActionStatus,
    AlertAction,
    AlertChatMessage,
    ChatRole,
)
from daalu_automation.models.billing import (
    InferenceTier,
    RoutingPolicy,
    Sku,
    TenantSku,
    UsageEvent,
)
from daalu_automation.models.briefing import Briefing, BriefingChannel, BriefingStatus
from daalu_automation.models.change_proposal import (
    ChangeProposal,
    ChangeProposalKind,
    ChangeProposalStatus,
)
from daalu_automation.models.cli_device import CliDeviceAuthorization
from daalu_automation.models.cluster_tunnel import ClusterTunnel, ClusterTunnelStatus
from daalu_automation.models.config_manager_tenant import (
    ConfigManagerTenant,
    ConfigManagerTenantState,
)
from daalu_automation.models.daalu_hosted_quota import DaaluHostedQuota
from daalu_automation.models.dashboard import Dashboard
from daalu_automation.models.email_verification import EmailVerificationToken
from daalu_automation.models.event import Event, EventSeverity
from daalu_automation.models.feedback import Feedback
from daalu_automation.models.gpu_diagnostic_run import (
    GpuDiagnosticKind,
    GpuDiagnosticRun,
    GpuDiagnosticState,
)
from daalu_automation.models.gpu_pool import GpuPool
from daalu_automation.models.gpu_revenue_share import GpuRevenueShare
from daalu_automation.models.gpu_tenant import GpuTenant, GpuTenantState
from daalu_automation.models.infra import Incident, IncidentSeverity, IncidentStatus, Service
from daalu_automation.models.integration import Integration, IntegrationStatus
from daalu_automation.models.invite import Invite
from daalu_automation.models.nautobot_tenant import (
    NautobotTenant,
    NautobotTenantState,
)
from daalu_automation.models.personal_access_token import PersonalAccessToken
from daalu_automation.models.recommendation import Recommendation, RecommendationStatus
from daalu_automation.models.report_schedule import ReportSchedule
from daalu_automation.models.saved_report import SavedReport
from daalu_automation.models.tenant import Tenant
from daalu_automation.models.user import User
from daalu_automation.models.workflow_run import WorkflowRun, WorkflowRunStatus
from daalu_automation.models.workspace import Workspace

__all__ = [
    "AgentRun",
    "ActionStatus",
    "Alert",
    "AlertAction",
    "AlertChatMessage",
    "AlertOccurrence",
    "AlertSeverity",
    "AlertStatus",
    "ChatRole",
    "AiperfRun",
    "AiperfRunState",
    "Briefing",
    "BriefingChannel",
    "BriefingStatus",
    "ChangeProposal",
    "ChangeProposalKind",
    "ChangeProposalStatus",
    "CliDeviceAuthorization",
    "ClusterTunnel",
    "ClusterTunnelStatus",
    "DaaluHostedQuota",
    "Dashboard",
    "EmailVerificationToken",
    "Event",
    "EventSeverity",
    "Feedback",
    "Incident",
    "IncidentSeverity",
    "IncidentStatus",
    "Integration",
    "IntegrationStatus",
    "Invite",
    "ConfigManagerTenant",
    "ConfigManagerTenantState",
    "GpuDiagnosticKind",
    "GpuDiagnosticRun",
    "GpuDiagnosticState",
    "GpuPool",
    "GpuRevenueShare",
    "GpuTenant",
    "GpuTenantState",
    "NautobotTenant",
    "NautobotTenantState",
    "PersonalAccessToken",
    "InferenceTier",
    "Recommendation",
    "RecommendationStatus",
    "ReportSchedule",
    "RoutingPolicy",
    "SavedReport",
    "Sku",
    "TenantSku",
    "UsageEvent",
    "Service",
    "Tenant",
    "User",
    "WorkflowRun",
    "WorkflowRunStatus",
    "Workspace",
]
