(function () {
    const root = document.querySelector("[data-admin-users-root]");
    if (!root) {
        return;
    }

    const state = {
        users: JSON.parse(root.dataset.users || "[]"),
        currentUserId: Number(root.dataset.currentUserId || "0"),
        editingUserId: null,
    };

    const elements = {
        rows: document.getElementById("user-rows"),
        addButton: document.getElementById("open-add-user-modal"),
        addModal: document.getElementById("add-user-modal"),
        editModal: document.getElementById("edit-user-modal"),
        addForm: document.getElementById("add-user-form"),
        editForm: document.getElementById("edit-user-form"),
        addError: document.getElementById("add-user-error"),
        editError: document.getElementById("edit-user-error"),
        addSubmit: document.getElementById("submit-add-user"),
        editSubmit: document.getElementById("submit-edit-user"),
        userCount: document.getElementById("user-count"),
        activeCount: document.getElementById("active-count"),
        adminCount: document.getElementById("admin-count"),
    };

    document.addEventListener("DOMContentLoaded", init);

    function init() {
        bindActions();
        renderUsers();
        updateSummary();
    }

    function bindActions() {
        elements.addButton?.addEventListener("click", () => openModal(elements.addModal));
        document.querySelectorAll("[data-close-modal]").forEach((button) => {
            button.addEventListener("click", () => closeModal(button.closest(".modal")));
        });
        document.querySelectorAll(".modal").forEach((modal) => {
            modal.addEventListener("click", (event) => {
                if (event.target.classList.contains("modal") || event.target.classList.contains("modal-backdrop")) {
                    closeModal(modal);
                }
            });
        });
        elements.addSubmit?.addEventListener("click", addUser);
        elements.editSubmit?.addEventListener("click", updateUser);
        document.querySelectorAll("[data-logout]").forEach((button) => {
            button.addEventListener("click", logout);
        });
    }

    function renderUsers() {
        if (!elements.rows) {
            return;
        }

        if (!state.users.length) {
            elements.rows.innerHTML = '<div class="empty-state">当前还没有可管理的用户。</div>';
            return;
        }

        elements.rows.innerHTML = state.users.map((user) => {
            const isSelf = user.id === state.currentUserId;
            return `
                <div class="user-row">
                    <div class="avatar">${escapeHtml(user.username.slice(0, 1).toUpperCase())}</div>
                    <div class="user-meta">
                        <strong>${escapeHtml(user.username)}</strong>
                        <span>ID ${user.id}${isSelf ? " · 当前账号" : ""}</span>
                    </div>
                    <div class="user-meta">
                        <strong>创建时间</strong>
                        <span>${escapeHtml(user.created_at)}</span>
                    </div>
                    <div>
                        <span class="badge ${user.is_admin ? "badge-admin" : "badge-user"}">${user.is_admin ? "管理员" : "普通用户"}</span>
                    </div>
                    <div>
                        <span class="badge ${user.is_active ? "badge-active" : "badge-inactive"}">${user.is_active ? "启用" : "禁用"}</span>
                    </div>
                    <div class="inline-actions">
                        <button class="btn btn-secondary" data-edit-user="${user.id}">编辑</button>
                        ${isSelf ? "" : `<button class="btn btn-danger" data-delete-user="${user.id}">删除</button>`}
                    </div>
                </div>
            `;
        }).join("");

        elements.rows.querySelectorAll("[data-edit-user]").forEach((button) => {
            button.addEventListener("click", () => openEditModal(Number(button.dataset.editUser)));
        });

        elements.rows.querySelectorAll("[data-delete-user]").forEach((button) => {
            button.addEventListener("click", () => deleteUser(Number(button.dataset.deleteUser)));
        });
    }

    function updateSummary() {
        const admins = state.users.filter((user) => user.is_admin).length;
        const actives = state.users.filter((user) => user.is_active).length;
        if (elements.userCount) {
            elements.userCount.textContent = String(state.users.length);
        }
        if (elements.activeCount) {
            elements.activeCount.textContent = String(actives);
        }
        if (elements.adminCount) {
            elements.adminCount.textContent = String(admins);
        }
    }

    function openModal(modal) {
        modal?.classList.add("is-open");
        document.body.style.overflow = "hidden";
    }

    function closeModal(modal) {
        modal?.classList.remove("is-open");
        document.body.style.overflow = "";
        clearErrors();
        if (modal === elements.addModal) {
            elements.addForm?.reset();
        }
        if (modal === elements.editModal) {
            elements.editForm?.reset();
        }
    }

    function clearErrors() {
        if (elements.addError) {
            elements.addError.hidden = true;
            elements.addError.textContent = "";
        }
        if (elements.editError) {
            elements.editError.hidden = true;
            elements.editError.textContent = "";
        }
    }

    function setError(target, message) {
        if (!target) {
            return;
        }
        target.hidden = false;
        target.textContent = message;
    }

    async function addUser() {
        clearErrors();
        const username = document.getElementById("add-username")?.value.trim() || "";
        const password = document.getElementById("add-password")?.value || "";
        const confirmPassword = document.getElementById("add-confirm-password")?.value || "";
        const isAdmin = document.getElementById("add-is-admin")?.checked || false;

        if (!username || !password) {
            setError(elements.addError, "用户名和密码不能为空。");
            return;
        }
        if (password !== confirmPassword) {
            setError(elements.addError, "两次输入的密码不一致。");
            return;
        }
        if (password.length < 6) {
            setError(elements.addError, "密码长度至少 6 位。");
            return;
        }

        elements.addSubmit.disabled = true;
        try {
            const result = await fetchJson("/api/admin/add_user", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    username,
                    password,
                    is_admin: isAdmin,
                }),
            });
            if (!result) {
                return;
            }
            if (!result.response.ok) {
                setError(elements.addError, result.json.error || "添加失败。");
                return;
            }
            state.users.unshift(normalizeUser(result.json.user));
            renderUsers();
            updateSummary();
            closeModal(elements.addModal);
        } catch (error) {
            setError(elements.addError, error.message);
        } finally {
            elements.addSubmit.disabled = false;
        }
    }

    function openEditModal(userId) {
        const user = state.users.find((entry) => entry.id === userId);
        if (!user) {
            return;
        }
        state.editingUserId = userId;
        document.getElementById("edit-user-id").value = String(user.id);
        document.getElementById("edit-username").value = user.username;
        document.getElementById("edit-password").value = "";
        document.getElementById("edit-is-admin").checked = user.is_admin;
        document.getElementById("edit-is-active").checked = user.is_active;

        const adminInput = document.getElementById("edit-is-admin");
        if (user.id === state.currentUserId) {
            adminInput.disabled = true;
        } else {
            adminInput.disabled = false;
        }
        openModal(elements.editModal);
    }

    async function updateUser() {
        clearErrors();
        const userId = state.editingUserId;
        if (!userId) {
            return;
        }

        const password = document.getElementById("edit-password")?.value || "";
        const isAdmin = document.getElementById("edit-is-admin")?.checked || false;
        const isActive = document.getElementById("edit-is-active")?.checked || false;
        const payload = {
            is_admin: isAdmin,
            is_active: isActive,
        };
        if (password) {
            if (password.length < 6) {
                setError(elements.editError, "密码长度至少 6 位。");
                return;
            }
            payload.password = password;
        }

        elements.editSubmit.disabled = true;
        try {
            const result = await fetchJson(`/api/admin/update_user/${userId}`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(payload),
            });
            if (!result) {
                return;
            }
            if (!result.response.ok) {
                setError(elements.editError, result.json.error || "更新失败。");
                return;
            }
            state.users = state.users.map((user) => user.id === userId ? normalizeUser(result.json.user) : user);
            renderUsers();
            updateSummary();
            closeModal(elements.editModal);
        } catch (error) {
            setError(elements.editError, error.message);
        } finally {
            elements.editSubmit.disabled = false;
        }
    }

    async function deleteUser(userId) {
        const user = state.users.find((entry) => entry.id === userId);
        if (!user) {
            return;
        }
        if (!window.confirm(`确定删除用户 "${user.username}" 吗？此操作不可撤销。`)) {
            return;
        }

        try {
            const result = await fetchJson(`/api/admin/delete_user/${userId}`, { method: "DELETE" });
            if (!result) {
                return;
            }
            if (!result.response.ok) {
                window.alert(result.json.error || "删除失败。");
                return;
            }
            state.users = state.users.filter((entry) => entry.id !== userId);
            renderUsers();
            updateSummary();
        } catch (error) {
            window.alert(error.message);
        }
    }

    async function fetchJson(url, options = {}) {
        const response = await fetch(url, {
            credentials: "include",
            ...options,
        });
        if (response.redirected || response.url.includes("/login")) {
            window.location.href = "/login";
            return null;
        }
        const json = await response.json();
        return { response, json };
    }

    function normalizeUser(user) {
        return {
            id: Number(user.id),
            username: user.username,
            created_at: user.created_at,
            is_admin: Boolean(user.is_admin),
            is_active: Boolean(user.is_active),
        };
    }

    function logout() {
        if (window.confirm("确定要退出登录吗？")) {
            window.location.href = "/logout";
        }
    }

    function escapeHtml(value) {
        return String(value)
            .replaceAll("&", "&amp;")
            .replaceAll("<", "&lt;")
            .replaceAll(">", "&gt;")
            .replaceAll('"', "&quot;")
            .replaceAll("'", "&#39;");
    }
})();
