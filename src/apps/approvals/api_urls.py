import django_filters
from django.core.exceptions import ValidationError as DjangoValidationError
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import filters, serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import SAFE_METHODS, BasePermission, IsAuthenticated
from rest_framework.response import Response
from rest_framework.routers import DefaultRouter

from apps.accounts.models import Role, User
from apps.accounts.permissions import IsAuditorReadOnly, IsNotVendorUser

from . import engine
from .models import ApprovalAction, ApprovalPolicy, ApprovalStep
from .services import (
    get_decision_target,
    process_approval_action_service,
    record_approval_action_service,
)


class CanManageApprovalPolicy(BasePermission):
    """
    Super Admin and Procurement Manager can manage approval policies.
    Internal staff can view them.
    External vendor users and requesters/auditors cannot mutate policies.
    """

    def has_permission(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return False
        if (
            getattr(request.user, "is_vendor", False)
            or getattr(request.user, "role_code", None) == Role.VENDOR_USER
        ):
            return False

        role_code = getattr(request.user, "role_code", None) or (
            request.user.role.code if getattr(request.user, "role", None) else None
        )
        if not request.user.is_superuser and role_code in [Role.STORES_RECEIVER, Role.LEGAL_MGR]:
            return False

        if request.method in SAFE_METHODS:
            return True
        return request.user.is_superuser or request.user.role_code in [
            Role.SUPER_ADMIN,
            Role.PROC_MGR,
        ]


class ApprovalStepSerializer(serializers.ModelSerializer):
    approver_role_code = serializers.CharField(source="approver_role.code", read_only=True)

    class Meta:
        model = ApprovalStep
        fields = [
            "step_number",
            "approver_role",
            "approver_role_code",
            "specific_approver",
            "description",
        ]


class ApprovalStepInputSerializer(serializers.Serializer):
    approver_role = serializers.SlugRelatedField(slug_field="code", queryset=Role.objects.all())
    specific_approver = serializers.PrimaryKeyRelatedField(
        queryset=User.objects.filter(is_active=True), required=False, allow_null=True
    )
    description = serializers.CharField(required=False, allow_blank=True, max_length=255)


class ApprovalPolicySerializer(serializers.ModelSerializer):
    """
    Policies are versioned and immutable once used: POST creates version 1, PUT/PATCH creates
    the next version (the response is the NEW version) and DELETE retires the policy.
    ``steps_input`` is the ordered approval chain: a list of
    {approver_role: <role code>, specific_approver?, description?}.
    """

    steps = ApprovalStepSerializer(many=True, read_only=True)
    steps_input = ApprovalStepInputSerializer(many=True, write_only=True, required=False)
    is_latest = serializers.SerializerMethodField()
    in_use = serializers.SerializerMethodField()

    class Meta:
        model = ApprovalPolicy
        fields = [
            "id",
            "name",
            "module",
            "department",
            "cost_center",
            "min_amount",
            "max_amount",
            "is_exception_route",
            "is_active",
            "policy_key",
            "version",
            "superseded_by",
            "is_latest",
            "in_use",
            "change_note",
            "steps",
            "steps_input",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["policy_key", "version", "superseded_by", "created_at", "updated_at"]

    def get_is_latest(self, obj):
        return obj.superseded_by_id is None

    def get_in_use(self, obj):
        return obj.is_in_use

    def validate(self, attrs):
        from decimal import Decimal

        min_amount = attrs.get("min_amount", getattr(self.instance, "min_amount", Decimal("0.00")))
        max_amount = attrs.get("max_amount", getattr(self.instance, "max_amount", None))
        if max_amount is not None and min_amount is not None and min_amount > max_amount:
            raise serializers.ValidationError(
                {"min_amount": ["Minimum amount cannot be greater than maximum amount."]}
            )
        if self.instance is None and not attrs.get("steps_input"):
            raise serializers.ValidationError(
                {"steps_input": ["An approval policy needs at least one approval step."]}
            )
        if (
            self.instance is not None
            and "module" in attrs
            and attrs["module"] != self.instance.module
        ):
            raise serializers.ValidationError(
                {"module": ["The transaction type of a policy cannot change; create a new policy."]}
            )
        return attrs


class ApprovalActionSerializer(serializers.ModelSerializer):
    actor_email = serializers.CharField(source="actor.email", read_only=True)

    class Meta:
        model = ApprovalAction
        fields = "__all__"


class ApprovalActionFilter(django_filters.FilterSet):
    status = django_filters.CharFilter(field_name="action", lookup_expr="iexact")
    action = django_filters.CharFilter(lookup_expr="iexact")
    target_model = django_filters.CharFilter(
        field_name="target_model_name", lookup_expr="icontains"
    )

    class Meta:
        model = ApprovalAction
        fields = ["status", "action", "target_model_name"]


class ApprovalPolicyViewSet(viewsets.ModelViewSet):
    queryset = (
        ApprovalPolicy.objects.select_related("department", "cost_center")
        .prefetch_related("steps__approver_role")
        .order_by("module", "name", "-version")
    )
    serializer_class = ApprovalPolicySerializer
    permission_classes = [IsAuthenticated, CanManageApprovalPolicy]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    search_fields = ["name", "module"]
    ordering_fields = ["name", "min_amount", "created_at", "version"]
    filterset_fields = ["module", "is_active", "is_exception_route", "department", "cost_center"]

    def get_queryset(self):
        from apps.core.tenancy import scope_approval_policies

        qs = scope_approval_policies(super().get_queryset(), self.request.user)
        if self.request.query_params.get("current", "").lower() in ("1", "true", "yes"):
            qs = qs.filter(superseded_by__isnull=True)
        return qs

    @staticmethod
    def _steps(validated):
        return [
            {
                "approver_role": step["approver_role"],
                "specific_approver": step.get("specific_approver"),
                "description": step.get("description", ""),
            }
            for step in validated
        ]

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        policy = engine.create_policy_with_steps(
            name=data["name"],
            module=data["module"],
            steps=self._steps(data["steps_input"]),
            min_amount=data.get("min_amount", 0),
            max_amount=data.get("max_amount"),
            department=data.get("department"),
            cost_center=data.get("cost_center"),
            is_exception_route=data.get("is_exception_route", False),
            is_active=data.get("is_active", True),
            actor=request.user,
            change_note=data.get("change_note", ""),
        )
        return Response(self.get_serializer(policy).data, status=status.HTTP_201_CREATED)

    def update(self, request, *args, **kwargs):
        policy = self.get_object()
        serializer = self.get_serializer(policy, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        changes = {
            key: data[key]
            for key in (
                "name",
                "min_amount",
                "max_amount",
                "department",
                "cost_center",
                "is_exception_route",
                "is_active",
            )
            if key in data
        }
        if "steps_input" in data:
            changes["steps"] = self._steps(data["steps_input"])
        new_version = engine.revise_approval_policy(
            policy=policy, actor=request.user, change_note=data.get("change_note", ""), **changes
        )
        return Response(self.get_serializer(new_version).data, status=status.HTTP_200_OK)

    def destroy(self, request, *args, **kwargs):
        # Policies are never deleted (requests and audit history reference them): retire instead.
        policy = engine.deactivate_approval_policy(
            policy=self.get_object(), actor=request.user, reason="Retired via API"
        )
        return Response(self.get_serializer(policy).data, status=status.HTTP_200_OK)


class ApprovalActionViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = ApprovalAction.objects.select_related("actor", "policy_step").order_by("-created_at")
    serializer_class = ApprovalActionSerializer
    permission_classes = [IsAuthenticated, IsNotVendorUser, IsAuditorReadOnly]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_class = ApprovalActionFilter
    search_fields = ["target_model_name", "comments", "actor__email"]
    ordering_fields = ["created_at", "action"]

    def get_queryset(self):
        from apps.core.tenancy import scope_approval_actions

        return scope_approval_actions(super().get_queryset(), self.request.user)

    def _decide(self, request, action_name, comment):
        action_obj = self.get_object()
        target = get_decision_target(action_obj.target_model_name, action_obj.target_object_id)
        if target is None:
            return Response(
                {
                    "error": {
                        "code": "NOT_DECIDABLE",
                        "message": (
                            f"{action_obj.target_model_name} records cannot be decided through "
                            "this endpoint."
                        ),
                    }
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            new_action = process_approval_action_service(
                target_object=target, actor=request.user, action=action_name, comments=comment
            )
        except DjangoValidationError as exc:
            msg = exc.messages[0] if getattr(exc, "messages", None) else str(exc)
            return Response(
                {"error": {"code": "APPROVAL_REFUSED", "message": msg}},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return Response(ApprovalActionSerializer(new_action).data, status=status.HTTP_200_OK)

    @action(detail=True, methods=["post"], url_path="approve")
    def approve(self, request, pk=None):
        return self._decide(request, ApprovalAction.ACTION_APPROVE, request.data.get("comment", ""))

    @action(detail=True, methods=["post"], url_path="reject")
    def reject(self, request, pk=None):
        reason = request.data.get("reason", request.data.get("comment", ""))
        return self._decide(request, ApprovalAction.ACTION_REJECT, reason)

    @action(detail=True, methods=["post"], url_path="comment")
    def comment(self, request, pk=None):
        """Comments are new, attributed, append-only entries: existing actions are never edited."""
        action_obj = self.get_object()
        comment_text = (request.data.get("comment") or "").strip()
        if not comment_text:
            return Response(
                {"error": {"code": "MISSING_PARAM", "message": "A comment is required."}},
                status=status.HTTP_400_BAD_REQUEST,
            )
        new_action = record_approval_action_service(
            target_object_id=action_obj.target_object_id,
            target_model_name=action_obj.target_model_name,
            actor=request.user,
            action=ApprovalAction.ACTION_COMMENT,
            previous_state=action_obj.new_state,
            new_state=action_obj.new_state,
            comments=comment_text,
            approval_request=action_obj.approval_request,
        )
        return Response(ApprovalActionSerializer(new_action).data, status=status.HTTP_201_CREATED)


router = DefaultRouter()
router.register(r"policies", ApprovalPolicyViewSet, basename="approval-policy")
router.register(r"actions", ApprovalActionViewSet, basename="approval-action")

urlpatterns = router.urls
