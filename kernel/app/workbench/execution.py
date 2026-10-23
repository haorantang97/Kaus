"""Trusted ownership-backed policy for restricted module conversations."""
from app.api.api_errors import ApiError


def install_execution_policy(service):
    def resolve(conversation_id):
        if not service.store.is_module_conversation(conversation_id):
            return None
        for job in service.store.all("jobs"):
            if job.get("executionPolicy", "project") == "project":
                continue
            owned = {identity for step in job["steps"] for identity in
                     [step.get("conversationId"), *step.get("previousConversationIds", [])] if identity}
            if conversation_id in owned:
                # IDs originate in the domain ID generator, never module paths.
                import hashlib
                root = service.root / "execution" / hashlib.sha256(conversation_id.encode()).hexdigest()
                root.mkdir(parents=True, exist_ok=True, mode=0o700)
                return {"policy": job["executionPolicy"], "workspace_root": str(root.resolve())}
        return None

    async def validate(conversation, policy):
        binding = await service.repositories.bindings.get(conversation.agent_binding_id)
        driver = service.registry.get(binding.backend_id) if binding else None
        supports = getattr(driver, "supports_execution_policy", None)
        if not supports or not supports(policy):
            raise ApiError(409, "execution_policy_unsupported", "当前 Agent 尚未适配此模块的执行范围。")
        return conversation

    service.host.execution_policy_resolver = resolve
    service.execution_policy_hook = validate
