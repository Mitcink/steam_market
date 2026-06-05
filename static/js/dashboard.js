(function () {
    const root = document.querySelector("[data-dashboard-root]");
    if (!root) {
        return;
    }

    const isAdmin = root.dataset.isAdmin === "true";

    const state = {
        itemsPerPage: 20,
        allData: [],
        viewData: [],
        currentPage: 1,
        sortKey: "update_time",
        sortDirection: "desc",
        filters: {},
        dataInterval: null,
        statusInterval: null,
        isLoading: false,
        lastDataHash: "",
        crawlerStatus: "stopped",
        activePage: null,
        sectionObserver: null,
        latestDate: "",
        filtersExpanded: true,
        credentialStatus: null,
        selectedItemIds: new Set(),
        previewedBuyItems: [],
        buffBuySettings: {},
    };

    const elements = {
        pageTabs: Array.from(document.querySelectorAll("[data-page-target]")),
        pages: Array.from(document.querySelectorAll("[data-page]")),
        statusPill: document.getElementById("crawler-status-pill"),
        heroStatus: document.getElementById("hero-status"),
        crawlerToggle: document.getElementById("crawler-toggle-btn"),
        refreshBtn: document.getElementById("refresh-data-btn"),
        clearButtons: Array.from(document.querySelectorAll("[data-clear-data]")),
        saveConfigBtn: document.getElementById("save-config-btn"),
        testConnectionBtn: document.getElementById("test-connection-btn"),
        clearBuffCredentialBtn: document.getElementById("clear-buff-credential-btn"),
        clearSteamCredentialBtn: document.getElementById("clear-steam-credential-btn"),
        previewBuffBuyBtn: document.getElementById("preview-buff-buy-btn"),
        executeBuffBuyBtn: document.getElementById("execute-buff-buy-btn"),
        selectAllItems: document.getElementById("select-all-items"),
        resetFilterBtn: document.getElementById("reset-filters-btn"),
        clearFilterBtn: document.getElementById("clear-filters-btn"),
        toggleFiltersBtn: document.getElementById("toggle-filters-btn"),
        filtersBlock: document.getElementById("filters-block"),
        filterPanelCopy: document.getElementById("filter-panel-copy"),
        crawlerLog: document.getElementById("crawler-log-box"),
        testLog: document.getElementById("test-log-box"),
        dataTableBody: document.getElementById("data-table-body"),
        pagination: document.getElementById("pagination-container"),
        totalItems: document.getElementById("total-items"),
        currentPageItems: document.getElementById("current-page-items"),
        candidateCount: document.getElementById("candidate-count"),
        lastUpdate: document.getElementById("last-update"),
        priceWindow: document.getElementById("price-window"),
        activityWindow: document.getElementById("activity-window"),
        dataSummary: document.getElementById("data-summary"),
        opportunitySummary: document.getElementById("opportunity-summary"),
        filterSummary: document.getElementById("filter-summary"),
        activeFilterChips: document.getElementById("active-filter-chips"),
        buffCredentialStatus: document.getElementById("buff-credential-status"),
        steamCredentialStatus: document.getElementById("steam-credential-status"),
        selectedCount: document.getElementById("selected-count"),
        buffBuyLog: document.getElementById("buff-buy-log-box"),
    };

    const configFieldIds = [
        "proxy-url",
        "max-threads",
        "min-sell-count",
        "min-sell-price",
        "max-sell-price",
        "web-auth",
        "buff-steamid",
        "buff-pay-method",
        "buff-allow-cooldown",
    ];

    const filterControlIds = [
        "filter-name",
        "filter-buff-price-primary",
        "filter-steam-price-primary",
        "filter-sell-ratio-primary",
        "filter-volume-primary",
        "filter-update-time-primary",
    ];

    document.addEventListener("DOMContentLoaded", init);

    async function init() {
        bindSectionNavigation();
        bindButtons();
        bindSorting();
        setupAutoSave();
        setupFilterControls();
        setupExternalActions();
        await loadConfig();
        await loadBuffBuySettings();
        activateInitialSection();
        resetFilters();
        checkCrawlerStatus();
        state.statusInterval = window.setInterval(checkCrawlerStatus, 10000);
        fetchData();
    }

    function bindSectionNavigation() {
        elements.pageTabs.forEach((link) => {
            link.addEventListener("click", (event) => {
                const targetId = link.dataset.pageTarget;
                const target = document.getElementById(targetId);
                if (!target) {
                    return;
                }
                event.preventDefault();
                activatePage(targetId, true);
                target.scrollIntoView({ behavior: "smooth", block: "start" });
            });
        });

        window.addEventListener("hashchange", () => {
            const sectionId = resolveSectionId();
            if (sectionId) {
                activatePage(sectionId, false);
            }
        });

        if (!("IntersectionObserver" in window) || !elements.pages.length) {
            return;
        }

        state.sectionObserver = new IntersectionObserver(
            (entries) => {
                const visibleSections = entries
                    .filter((entry) => entry.isIntersecting)
                    .sort((left, right) => right.intersectionRatio - left.intersectionRatio);

                if (!visibleSections.length) {
                    return;
                }

                activatePage(visibleSections[0].target.id, false);
            },
            {
                root: null,
                rootMargin: "-18% 0px -55% 0px",
                threshold: [0.2, 0.4, 0.6],
            },
        );

        elements.pages.forEach((page) => state.sectionObserver.observe(page));
    }

    function activatePage(pageName, syncHash) {
        if (!pageName) {
            return;
        }
        state.activePage = pageName;
        elements.pageTabs.forEach((tab) => {
            tab.classList.toggle("is-active", tab.dataset.pageTarget === pageName);
        });
        if (syncHash && window.location.hash !== `#${pageName}`) {
            history.replaceState(null, "", `#${pageName}`);
        }
    }

    function activateInitialSection() {
        activatePage(resolveSectionId() || (isAdmin ? "overview" : "data"), false);
    }

    function resolveSectionId() {
        const hash = window.location.hash.replace("#", "");
        const validPages = elements.pages.map((page) => page.dataset.page);
        return hash && validPages.includes(hash) ? hash : null;
    }

    function bindButtons() {
        if (elements.crawlerToggle) {
            elements.crawlerToggle.addEventListener("click", toggleCrawler);
        }
        if (elements.refreshBtn) {
            elements.refreshBtn.addEventListener("click", fetchData);
        }
        elements.clearButtons.forEach((button) => button.addEventListener("click", clearData));
        if (elements.saveConfigBtn) {
            elements.saveConfigBtn.addEventListener("click", async () => {
                await saveConfig(getFormConfig(), true);
            });
        }
        if (elements.testConnectionBtn) {
            elements.testConnectionBtn.addEventListener("click", testConnection);
        }
        if (elements.clearBuffCredentialBtn) {
            elements.clearBuffCredentialBtn.addEventListener("click", () => clearCredentials(["buff_auth"]));
        }
        if (elements.clearSteamCredentialBtn) {
            elements.clearSteamCredentialBtn.addEventListener("click", () => clearCredentials(["steam_cookies"]));
        }
        if (elements.previewBuffBuyBtn) {
            elements.previewBuffBuyBtn.addEventListener("click", previewBuffBuys);
        }
        if (elements.executeBuffBuyBtn) {
            elements.executeBuffBuyBtn.addEventListener("click", executeBuffBuys);
        }
        if (elements.selectAllItems) {
            elements.selectAllItems.addEventListener("change", toggleSelectAllCurrentPage);
        }
        if (elements.resetFilterBtn) {
            elements.resetFilterBtn.addEventListener("click", resetFilters);
        }
        if (elements.clearFilterBtn) {
            elements.clearFilterBtn.addEventListener("click", clearFilters);
        }
        if (elements.toggleFiltersBtn) {
            elements.toggleFiltersBtn.addEventListener("click", toggleFiltersPanel);
        }
        document.querySelectorAll("[data-logout]").forEach((button) => {
            button.addEventListener("click", logout);
        });
    }

    function bindSorting() {
        document.querySelectorAll("[data-sort-key]").forEach((button) => {
            button.addEventListener("click", () => {
                const key = button.dataset.sortKey;
                if (state.sortKey === key) {
                    state.sortDirection = state.sortDirection === "asc" ? "desc" : "asc";
                } else {
                    state.sortKey = key;
                    state.sortDirection = key === "name" ? "asc" : "desc";
                }
                rebuildViewData();
                renderTable();
                renderPagination();
                updateDashboardMetrics();
            });
        });
    }

    function setupAutoSave() {
        configFieldIds.forEach((id) => {
            const element = document.getElementById(id);
            if (!element) {
                return;
            }
            const handler = () => {
                window.localStorage.setItem(getStorageKey(), JSON.stringify(splitLocalConfig(getFormConfig())));
                updateOverviewCards();
            };
            element.addEventListener(element.type === "checkbox" ? "change" : "input", handler);
        });
    }

    function setupFilterControls() {
        filterControlIds.forEach((id) => {
            const element = document.getElementById(id);
            if (!element) {
                return;
            }

            const eventName = element.type === "date" ? "change" : "input";
            element.addEventListener(eventName, applyFilters);
            element.addEventListener("keydown", (event) => {
                if (event.key === "Enter") {
                    event.preventDefault();
                }
            });
        });
    }

    function setupExternalActions() {
        document.querySelectorAll("[data-open-external]").forEach((button) => {
            button.addEventListener("click", () => {
                const url = button.dataset.openExternal;
                window.open(url, "_blank", "noopener,noreferrer");
            });
        });

        document.querySelectorAll("[data-toggle-guide]").forEach((button) => {
            button.addEventListener("click", () => {
                const guideKey = button.dataset.toggleGuide;
                const panel = document.getElementById(`guide-${guideKey}`);
                if (!panel) {
                    return;
                }
                const shouldOpen = panel.hidden;
                document.querySelectorAll(".token-guide-panel").forEach((item) => {
                    item.hidden = true;
                });
                panel.hidden = !shouldOpen;
            });
        });
    }

    function getStorageKey() {
        return "steam_market_dashboard_config_v4";
    }

    function getFormConfig() {
        return {
            proxy_url: document.getElementById("proxy-url")?.value.trim() || "",
            buff_auth: document.getElementById("buff-auth")?.value.trim() || "",
            steam_cookies: document.getElementById("steam-cookies")?.value.trim() || "",
            max_threads: document.getElementById("max-threads")?.value || "2",
            game_type: document.querySelector('input[name="game_type"]:checked')?.value || "CSGO",
            min_sell_price: document.getElementById("min-sell-price")?.value || "25",
            max_sell_price: document.getElementById("max-sell-price")?.value || "300",
            min_sell_count: document.getElementById("min-sell-count")?.value || "20",
            web_auth: document.getElementById("web-auth")?.checked || false,
        };
    }

    function splitLocalConfig(config) {
        return {
            proxy_url: config.proxy_url,
            max_threads: config.max_threads,
            game_type: config.game_type,
            min_sell_price: config.min_sell_price,
            max_sell_price: config.max_sell_price,
            min_sell_count: config.min_sell_count,
            web_auth: config.web_auth,
        };
    }

    function getBuffBuySettings() {
        return {
            steamid: document.getElementById("buff-steamid")?.value.trim() || "",
            pay_method: document.getElementById("buff-pay-method")?.value.trim() || "44",
            allow_tradable_cooldown: document.getElementById("buff-allow-cooldown")?.value.trim() || "0",
        };
    }

    async function saveConfig(config, shouldToast) {
        window.localStorage.setItem(getStorageKey(), JSON.stringify(splitLocalConfig(config)));
        let saved = false;
        try {
            const result = await fetchJson("/api/config", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(config),
            });
            if (!result) {
                return;
            }
            applyServerConfig(result.json.config);
            saved = true;
            if (shouldToast) {
                writeLog(elements.crawlerLog, "success", result.json.message || "配置已保存到服务器。站点凭证不会在页面回显。");
            }
        } catch (error) {
            writeLog(elements.crawlerLog, "error", `保存配置失败: ${error.message}`);
        }
        if (saved) {
            clearCredentialInputs();
        }
        updateOverviewCards();
    }

    async function loadConfig() {
        const raw = window.localStorage.getItem(getStorageKey());
        if (raw) {
            try {
                const config = JSON.parse(raw);
                applyConfigToForm(config);
            } catch (error) {
                console.error("Failed to parse saved config", error);
            }
        }

        try {
            const result = await fetchJson("/api/config");
            if (result?.json?.config) {
                applyServerConfig(result.json.config);
            }
        } catch (error) {
            writeLog(elements.crawlerLog, "error", `加载服务器配置失败: ${error.message}`);
        }
        clearCredentialInputs();
        updateOverviewCards();
    }

    function applyConfigToForm(config) {
        const mapping = {
            "proxy-url": config.proxy_url,
            "max-threads": config.max_threads,
            "min-sell-count": config.min_sell_count,
            "min-sell-price": config.min_sell_price,
            "max-sell-price": config.max_sell_price,
        };

        Object.entries(mapping).forEach(([id, value]) => {
            const element = document.getElementById(id);
            if (element && value !== undefined && value !== null) {
                element.value = value;
            }
        });

        const webAuth = document.getElementById("web-auth");
        if (webAuth) {
            webAuth.checked = Boolean(config.web_auth);
        }

        const radio = document.querySelector(`input[name="game_type"][value="${config.game_type || "CSGO"}"]`);
        if (radio) {
            radio.checked = true;
        }
    }

    async function loadBuffBuySettings() {
        if (!isAdmin) {
            return;
        }
        try {
            const result = await fetchJson("/api/buff_buy_settings");
            if (!result?.json?.settings) {
                return;
            }
            state.buffBuySettings = result.json.settings;
            applyBuffBuySettings(result.json.settings);
        } catch (error) {
            writeLog(elements.crawlerLog, "error", `加载 BUFF 购买设置失败: ${error.message}`);
        }
    }

    function applyBuffBuySettings(settings) {
        const steamid = document.getElementById("buff-steamid");
        const payMethod = document.getElementById("buff-pay-method");
        const allowCooldown = document.getElementById("buff-allow-cooldown");
        if (steamid && settings.steamid) {
            steamid.value = settings.steamid;
        }
        if (payMethod && settings.pay_method) {
            payMethod.value = settings.pay_method;
        }
        if (allowCooldown && settings.allow_tradable_cooldown !== undefined) {
            allowCooldown.value = settings.allow_tradable_cooldown;
        }
    }

    async function saveBuffBuySettings() {
        const settings = getBuffBuySettings();
        const result = await fetchJson("/api/buff_buy_settings", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(settings),
        });
        if (result?.json?.settings) {
            state.buffBuySettings = result.json.settings;
        }
        return settings;
    }

    function applyServerConfig(payload) {
        if (!payload) {
            return;
        }
        state.credentialStatus = payload;
        if (payload.settings) {
            applyConfigToForm(payload.settings);
            window.localStorage.setItem(getStorageKey(), JSON.stringify(splitLocalConfig(payload.settings)));
        }
        renderCredentialStatus();
    }

    function renderCredentialStatus() {
        if (elements.buffCredentialStatus) {
            const text = getCredentialStatusText(state.credentialStatus?.buff);
            elements.buffCredentialStatus.textContent = text;
        }
        if (elements.steamCredentialStatus) {
            const text = getCredentialStatusText(state.credentialStatus?.steam);
            elements.steamCredentialStatus.textContent = text;
        }
        updateProtectedFieldPlaceholders();
    }

    function getCredentialStatusText(entry) {
        if (!entry?.configured) {
            return "未保存";
        }
        const health = entry.health || {};
        if (health.status === "healthy" && health.checked_at) {
            return `已保存 · 最近检测通过 (${health.checked_at})`;
        }
        if (health.status === "expired_or_invalid" && health.checked_at) {
            return `已保存 · 最近检测失败，可能已过期 (${health.checked_at})`;
        }
        return "已保存 · 待检测";
    }

    function clearCredentialInputs() {
        const buff = document.getElementById("buff-auth");
        const steam = document.getElementById("steam-cookies");
        if (buff) {
            buff.value = "";
        }
        if (steam) {
            steam.value = "";
        }
        updateProtectedFieldPlaceholders();
    }

    function updateProtectedFieldPlaceholders() {
        setProtectedFieldPlaceholder(
            "buff-auth",
            state.credentialStatus?.buff?.configured,
            "这里既可以粘贴 session，也可以直接粘贴完整 BUFF Cookie 字符串",
            "已保存到服务器；如需替换，重新粘贴新的 session 或完整 BUFF Cookie",
        );
        setProtectedFieldPlaceholder(
            "steam-cookies",
            state.credentialStatus?.steam?.configured,
            "登录 Steam 后，将需要的 Cookie 字符串粘贴到这里",
            "已保存到服务器；如需替换，重新粘贴新的 Steam Cookie 字符串",
        );
    }

    function setProtectedFieldPlaceholder(fieldId, configured, emptyPlaceholder, savedPlaceholder) {
        const field = document.getElementById(fieldId);
        if (!field) {
            return;
        }
        if (field.value.trim()) {
            return;
        }
        field.placeholder = configured ? savedPlaceholder : emptyPlaceholder;
    }

    function updateOverviewCards() {
        const config = getFormConfig();
        if (elements.priceWindow) {
            elements.priceWindow.textContent = `¥${config.min_sell_price} - ¥${config.max_sell_price}`;
        }
        if (elements.activityWindow) {
            elements.activityWindow.textContent = `${config.min_sell_count}+ 在售`;
        }
    }

    async function checkResponse(response) {
        if (response.redirected || response.url.includes("/login")) {
            window.location.href = "/login";
            return null;
        }

        const contentType = response.headers.get("content-type") || "";
        if (!contentType.includes("application/json")) {
            window.location.href = "/login";
            return null;
        }
        return response;
    }

    async function fetchJson(url, options = {}) {
        const response = await fetch(url, {
            credentials: "include",
            ...options,
        });
        const validResponse = await checkResponse(response);
        if (!validResponse) {
            return null;
        }
        return { response: validResponse, json: await validResponse.json() };
    }

    async function testConnection() {
        const config = getFormConfig();
        writeLog(elements.testLog, "info", "开始检测 Steam 和 Buff 连通性...");
        elements.testConnectionBtn.disabled = true;

        try {
            await saveConfig(config, false);
            const result = await fetchJson("/api/test_connection", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(config),
            });
            if (!result) {
                return;
            }

            const { json } = result;
            if (json.error) {
                writeLog(elements.testLog, "error", `检测失败: ${json.error}`);
                return;
            }

            if (json.config) {
                applyServerConfig(json.config);
            }
            writeLog(elements.testLog, json.steam?.success ? "success" : "error", `Steam: ${json.steam?.message || "无返回"}`);
            writeLog(elements.testLog, json.buff?.success ? "success" : "error", `Buff: ${json.buff?.message || "无返回"}`);
        } catch (error) {
            writeLog(elements.testLog, "error", `检测失败: ${error.message}`);
        } finally {
            elements.testConnectionBtn.disabled = false;
        }
    }

    async function toggleCrawler() {
        if (state.crawlerStatus === "running") {
            await stopCrawler();
        } else {
            await startCrawler();
        }
    }

    async function startCrawler() {
        const config = getFormConfig();
        await saveConfig(config, false);
        writeLog(elements.crawlerLog, "info", "提交启动请求...");
        elements.crawlerToggle.disabled = true;

        try {
            const result = await fetchJson("/api/start_crawler", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(config),
            });
            if (!result) {
                return;
            }
            writeLog(elements.crawlerLog, result.response.ok ? "success" : "warn", result.json.message || "启动请求已返回。");
            if (result.response.ok) {
                setCrawlerStatus("running");
                ensureDataPolling();
            }
        } catch (error) {
            writeLog(elements.crawlerLog, "error", `启动失败: ${error.message}`);
        } finally {
            elements.crawlerToggle.disabled = false;
        }
    }

    async function stopCrawler() {
        writeLog(elements.crawlerLog, "warn", "提交停止请求...");
        elements.crawlerToggle.disabled = true;

        try {
            const result = await fetchJson("/api/stop_crawler", { method: "POST" });
            if (!result) {
                return;
            }
            writeLog(elements.crawlerLog, result.response.ok ? "success" : "warn", result.json.message || "停止请求已返回。");
            if (result.response.ok) {
                setCrawlerStatus("stopped");
                stopDataPolling();
                window.setTimeout(fetchData, 1200);
            }
        } catch (error) {
            writeLog(elements.crawlerLog, "error", `停止失败: ${error.message}`);
        } finally {
            elements.crawlerToggle.disabled = false;
        }
    }

    async function checkCrawlerStatus() {
        try {
            const result = await fetchJson("/api/status");
            if (!result) {
                return;
            }
            setCrawlerStatus(result.json.status || "stopped");
            if (state.crawlerStatus === "running") {
                ensureDataPolling();
            } else {
                stopDataPolling();
            }
        } catch (error) {
            console.error("Failed to check crawler status", error);
        }
    }

    function setCrawlerStatus(status) {
        state.crawlerStatus = status;
        const running = status === "running";

        if (elements.statusPill) {
            elements.statusPill.textContent = running ? "爬虫运行中" : "爬虫待命";
            elements.statusPill.className = `status-pill ${running ? "success" : "warn"}`;
        }
        if (elements.heroStatus) {
            elements.heroStatus.textContent = running ? "正在抓取实时机会" : "等待下一次任务";
        }
        if (elements.crawlerToggle) {
            elements.crawlerToggle.textContent = running ? "停止抓取" : "启动抓取";
            elements.crawlerToggle.className = `btn ${running ? "btn-danger" : "btn-primary"}`;
        }
    }

    function ensureDataPolling() {
        if (!state.dataInterval) {
            state.dataInterval = window.setInterval(fetchData, 5000);
        }
    }

    function stopDataPolling() {
        if (state.dataInterval) {
            window.clearInterval(state.dataInterval);
            state.dataInterval = null;
        }
    }

    async function clearCredentials(fields) {
        if (!window.confirm("确定要清除已保存的站点凭证吗？")) {
            return;
        }

        try {
            const result = await fetchJson("/api/config/credentials", {
                method: "DELETE",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ fields }),
            });
            if (!result) {
                return;
            }
            applyServerConfig(result.json.config);
            clearCredentialInputs();
            writeLog(elements.crawlerLog, "success", result.json.message || "凭证已清除。");
        } catch (error) {
            writeLog(elements.crawlerLog, "error", `清除凭证失败: ${error.message}`);
        }
    }

    function getSelectedItems() {
        return state.allData.filter((item) => state.selectedItemIds.has(String(item.buff_id)));
    }

    function toggleItemSelection(itemId, checked) {
        const normalized = String(itemId);
        if (checked) {
            state.selectedItemIds.add(normalized);
        } else {
            state.selectedItemIds.delete(normalized);
        }
        updateSelectionSummary();
        syncSelectAllCheckbox();
    }

    function toggleSelectAllCurrentPage() {
        const checked = Boolean(elements.selectAllItems?.checked);
        getCurrentPageData().forEach((item) => {
            const normalized = String(item.buff_id);
            if (checked) {
                state.selectedItemIds.add(normalized);
            } else {
                state.selectedItemIds.delete(normalized);
            }
        });
        renderTable();
        updateSelectionSummary();
    }

    function updateSelectionSummary() {
        if (elements.selectedCount) {
            elements.selectedCount.textContent = String(state.selectedItemIds.size);
        }
    }

    function syncSelectAllCheckbox() {
        if (!elements.selectAllItems) {
            return;
        }
        const pageData = getCurrentPageData();
        if (!pageData.length) {
            elements.selectAllItems.checked = false;
            elements.selectAllItems.indeterminate = false;
            return;
        }
        const selectedOnPage = pageData.filter((item) => state.selectedItemIds.has(String(item.buff_id))).length;
        elements.selectAllItems.checked = selectedOnPage === pageData.length;
        elements.selectAllItems.indeterminate = selectedOnPage > 0 && selectedOnPage < pageData.length;
    }

    async function previewBuffBuys() {
        const items = getSelectedItems();
        if (!items.length) {
            writeLog(elements.buffBuyLog, "warn", "请先勾选至少一个饰品。");
            return;
        }

        try {
            await saveConfig(getFormConfig(), false);
            const buySettings = await saveBuffBuySettings();
            writeLog(elements.buffBuyLog, "info", `开始预检 ${items.length} 个饰品...`);
            const result = await fetchJson("/api/buff/buy_preview_batch", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    config: getFormConfig(),
                    buy_settings: buySettings,
                    items: items.map((item) => ({
                        buff_id: item.buff_id,
                        goods_id: item.buff_id,
                        name: item.name,
                        game_type: item.kind,
                        target_price: item.buff_sell_price,
                        buff_sell_price: item.buff_sell_price,
                    })),
                }),
            });
            if (!result) {
                return;
            }
            state.previewedBuyItems = result.json.data || [];
            const successCount = state.previewedBuyItems.filter((item) => item.success).length;
            writeLog(elements.buffBuyLog, successCount ? "success" : "warn", `预检完成: ${successCount}/${items.length} 项可下单。`);
            state.previewedBuyItems.forEach((item) => {
                if (item.success) {
                    writeLog(elements.buffBuyLog, "info", `${item.name}: 卖单 ${item.sell_order_id} / ¥${Number(item.price).toFixed(2)} / ${item.pay_method_description || "支付方式已确认"}`);
                } else {
                    writeLog(elements.buffBuyLog, "error", `${item.name || item.goods_id}: ${item.error}`);
                }
            });
        } catch (error) {
            writeLog(elements.buffBuyLog, "error", `批量预检失败: ${error.message}`);
        }
    }

    async function executeBuffBuys() {
        const buyableItems = (state.previewedBuyItems || []).filter((item) => item.success);
        if (!buyableItems.length) {
            writeLog(elements.buffBuyLog, "warn", "没有可执行的预检结果，请先批量预检。");
            return;
        }
        if (!window.confirm(`确定要提交 ${buyableItems.length} 个 BUFF 购买请求吗？`)) {
            return;
        }

        try {
            const buySettings = await saveBuffBuySettings();
            writeLog(elements.buffBuyLog, "info", `开始提交 ${buyableItems.length} 个购买请求...`);
            const result = await fetchJson("/api/buff/buy_batch", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    config: getFormConfig(),
                    buy_settings: buySettings,
                    items: buyableItems,
                }),
            });
            if (!result) {
                return;
            }
            const rows = result.json.data || [];
            const successCount = rows.filter((item) => item.success).length;
            writeLog(elements.buffBuyLog, successCount ? "success" : "warn", `批量下单完成: ${successCount}/${buyableItems.length} 成功。`);
            rows.forEach((item) => {
                if (item.success) {
                    writeLog(elements.buffBuyLog, "success", `${item.name}: 下单成功，保护期提醒时间 ${item.release_at_utc}`);
                } else {
                    writeLog(elements.buffBuyLog, "error", `${item.name || item.goods_id}: ${item.error}`);
                }
            });
        } catch (error) {
            writeLog(elements.buffBuyLog, "error", `批量下单失败: ${error.message}`);
        }
    }

    async function clearData() {
        if (!window.confirm("确定要清空数据库中的全部市场数据吗？")) {
            return;
        }

        writeLog(elements.crawlerLog, "warn", "正在清空数据...");
        setClearButtonsDisabled(true);
        try {
            const result = await fetchJson("/api/clear_data", { method: "POST" });
            if (!result) {
                return;
            }
            writeLog(elements.crawlerLog, result.response.ok ? "success" : "error", result.json.message || "清空操作已完成。");
            if (result.response.ok) {
                state.allData = [];
                state.latestDate = "";
                rebuildViewData();
                updateDashboardMetrics();
                renderTable();
                renderPagination();
            }
        } catch (error) {
            writeLog(elements.crawlerLog, "error", `清空失败: ${error.message}`);
        } finally {
            setClearButtonsDisabled(false);
        }
    }

    async function fetchData() {
        if (state.isLoading) {
            return;
        }
        state.isLoading = true;

        try {
            const result = await fetchJson("/api/data");
            if (!result) {
                return;
            }

            const payload = result.json;
            if (!payload.success || !Array.isArray(payload.data)) {
                writeLog(elements.crawlerLog, "error", payload.error || "数据格式不正确。");
                return;
            }

            state.allData = payload.data;
            state.latestDate = payload.meta?.latest_date || "";
            syncDefaultUpdateDate();

            const nextHash = `${payload.data.length}:${payload.data[0]?.update_time || "none"}:${payload.data[payload.data.length - 1]?.buff_id || "none"}`;
            if (nextHash !== state.lastDataHash) {
                state.lastDataHash = nextHash;
                rebuildViewData();
                updateDashboardMetrics();
                renderTable();
                renderPagination();
            }
        } catch (error) {
            writeLog(elements.crawlerLog, "error", `加载数据失败: ${error.message}`);
        } finally {
            state.isLoading = false;
        }
    }

    function applyFilters() {
        state.currentPage = 1;
        rebuildViewData();
        updateDashboardMetrics();
        renderTable();
        renderPagination();
        syncSelectAllCheckbox();
    }

    function rebuildViewData() {
        state.filters = collectFilters();
        state.viewData = state.allData
            .filter((item) => itemMatchesFilters(item, state.filters))
            .sort((left, right) => compareValues(getSortValue(left, state.sortKey), getSortValue(right, state.sortKey), state.sortDirection));
    }

    function collectFilters() {
        return {
            name: getInputValue("filter-name").toLowerCase(),
            buffPrice: getThresholdFilter("filter-buff-price-primary", "min"),
            steamPrice: getThresholdFilter("filter-steam-price-primary", "min"),
            sellRatio: getThresholdFilter("filter-sell-ratio-primary", "max"),
            volume: getThresholdFilter("filter-volume-primary", "min"),
            updateTime: getDateThresholdFilter("filter-update-time-primary", "after"),
        };
    }

    function getThresholdFilter(inputId, mode) {
        const value = getNumberValue(inputId);
        return value === null || Number.isNaN(value) ? null : { mode, primary: value };
    }

    function getDateThresholdFilter(inputId, mode) {
        const value = getDateValue(inputId);
        return value ? { mode, primary: value } : null;
    }

    function itemMatchesFilters(item, filters) {
        if (filters.name && !String(item.name || "").toLowerCase().includes(filters.name)) {
            return false;
        }
        if (!matchesThreshold(Number(item.buff_sell_price), filters.buffPrice)) {
            return false;
        }
        if (!matchesThreshold(Number(item.steam_sell_price), filters.steamPrice)) {
            return false;
        }
        if (!matchesThreshold(computeSellRatio(item), filters.sellRatio)) {
            return false;
        }
        if (!matchesThreshold(Number(item.steam_volume), filters.volume)) {
            return false;
        }
        if (!matchesDateThreshold(parseDateOnly(item.update_time), filters.updateTime)) {
            return false;
        }
        return true;
    }

    function matchesThreshold(value, filter) {
        if (!filter) {
            return true;
        }
        if (value === null || value === undefined || Number.isNaN(value)) {
            return false;
        }
        return filter.mode === "max" ? value <= filter.primary : value >= filter.primary;
    }

    function matchesDateThreshold(value, filter) {
        if (!filter) {
            return true;
        }
        if (!value) {
            return false;
        }
        return value >= filter.primary;
    }

    function updateDashboardMetrics() {
        const data = getCurrentData();
        const totalCount = data.length;
        const candidateCount = data.filter((item) => {
            const ratio = computeSellRatio(item);
            return ratio !== null && ratio <= 0.8;
        }).length;
        const latest = data[0]?.update_time || "--";

        if (elements.totalItems) {
            elements.totalItems.textContent = String(totalCount);
        }
        if (elements.currentPageItems) {
            elements.currentPageItems.textContent = String(getCurrentPageData().length);
        }
        if (elements.candidateCount) {
            elements.candidateCount.textContent = String(candidateCount);
        }
        if (elements.lastUpdate) {
            elements.lastUpdate.textContent = latest;
        }
        if (elements.dataSummary) {
            elements.dataSummary.textContent = totalCount
                ? `当前展示 ${totalCount} 条记录，按 ${state.sortKey} ${state.sortDirection === "asc" ? "升序" : "降序"} 排列。`
                : "还没有可展示的市场数据。";
        }
        if (elements.opportunitySummary) {
            elements.opportunitySummary.textContent = candidateCount
                ? `检测到 ${candidateCount} 条比例不高于 0.80 的候选结果。`
                : "暂未发现满足当前标准的候选结果。";
        }
        if (elements.filterSummary) {
            const activeCount = countActiveFilters(state.filters);
            elements.filterSummary.textContent = activeCount ? `当前使用 ${activeCount} 项筛选条件` : "当前使用默认筛选";
        }
        if (elements.filterPanelCopy) {
            elements.filterPanelCopy.textContent = state.latestDate
                ? `当前默认只显示 ${state.latestDate} 当天结果，可展开修改。`
                : "默认按最新日期和业务阈值筛选，输入后立即生效。";
        }

        renderActiveFilterChips();
    }

    function countActiveFilters(filters) {
        let count = 0;
        if (filters.name) {
            count += 1;
        }
        if (filters.buffPrice && filters.buffPrice.primary !== 20) {
            count += 1;
        }
        if (filters.steamPrice) {
            count += 1;
        }
        if (filters.sellRatio && filters.sellRatio.primary !== 0.8) {
            count += 1;
        }
        if (filters.volume && filters.volume.primary !== 20) {
            count += 1;
        }
        if (filters.updateTime && filters.updateTime.primary !== state.latestDate) {
            count += 1;
        }
        return count;
    }

    function renderActiveFilterChips() {
        if (!elements.activeFilterChips) {
            return;
        }

        const chips = [];
        if (state.filters.name) {
            chips.push(renderChip(`名称 ${state.filters.name}`));
        }
        if (state.filters.buffPrice && state.filters.buffPrice.primary !== 20) {
            chips.push(renderChip(`BUFF 出售价(≥) ${state.filters.buffPrice.primary}`));
        }
        if (state.filters.steamPrice) {
            chips.push(renderChip(`Steam 出售价(≥) ${state.filters.steamPrice.primary}`));
        }
        if (state.filters.sellRatio && state.filters.sellRatio.primary !== 0.8) {
            chips.push(renderChip(`出售价比例(≤) ${state.filters.sellRatio.primary}`));
        }
        if (state.filters.volume && state.filters.volume.primary !== 20) {
            chips.push(renderChip(`Steam 成交量(≥) ${state.filters.volume.primary}`));
        }
        if (state.filters.updateTime && state.filters.updateTime.primary !== state.latestDate) {
            chips.push(renderChip(`更新时间(≥) ${state.filters.updateTime.primary}`));
        }

        if (!chips.length) {
            elements.activeFilterChips.innerHTML = "";
            elements.activeFilterChips.hidden = true;
            return;
        }

        elements.activeFilterChips.hidden = false;
        elements.activeFilterChips.innerHTML = chips.join("");
    }

    function renderChip(text) {
        return `<span class="filter-chip">${escapeHtml(text)}</span>`;
    }

    function getCurrentData() {
        return state.viewData;
    }

    function getCurrentPageData() {
        const start = (state.currentPage - 1) * state.itemsPerPage;
        return getCurrentData().slice(start, start + state.itemsPerPage);
    }

    function resetFilters() {
        setControlValue("filter-name", "");
        setControlValue("filter-buff-price-primary", "20");
        setControlValue("filter-steam-price-primary", "");
        setControlValue("filter-sell-ratio-primary", "0.8");
        setControlValue("filter-volume-primary", "20");
        syncDefaultUpdateDate(true);
        applyFilters();
    }

    function clearFilters() {
        setControlValue("filter-name", "");
        setControlValue("filter-buff-price-primary", "");
        setControlValue("filter-steam-price-primary", "");
        setControlValue("filter-sell-ratio-primary", "");
        setControlValue("filter-volume-primary", "");
        setControlValue("filter-update-time-primary", "");
        applyFilters();
    }

    function syncDefaultUpdateDate(force) {
        const field = document.getElementById("filter-update-time-primary");
        if (!field || !state.latestDate) {
            return;
        }
        if (force || !field.value) {
            field.value = state.latestDate;
        }
    }

    function toggleFiltersPanel() {
        if (!elements.filtersBlock || !elements.toggleFiltersBtn) {
            return;
        }
        state.filtersExpanded = !state.filtersExpanded;
        elements.filtersBlock.hidden = !state.filtersExpanded;
        elements.filtersBlock.classList.toggle("is-collapsed", !state.filtersExpanded);
        elements.toggleFiltersBtn.textContent = state.filtersExpanded ? "收起筛选" : "展开筛选";
        elements.toggleFiltersBtn.setAttribute("aria-expanded", state.filtersExpanded ? "true" : "false");
    }

    function setControlValue(id, value) {
        const element = document.getElementById(id);
        if (element) {
            element.value = value;
        }
    }

    function getInputValue(id) {
        return document.getElementById(id)?.value.trim() || "";
    }

    function getNumberValue(id) {
        const raw = getInputValue(id);
        return raw === "" ? null : Number(raw);
    }

    function getDateValue(id) {
        const raw = getInputValue(id);
        return raw === "" ? null : raw;
    }

    function getSortValue(item, key) {
        if (key === "steam_sell_ratio") {
            return computeSellRatio(item);
        }
        if (key === "update_time") {
            return item.update_time || "";
        }
        return item[key];
    }

    function compareValues(left, right, direction) {
        const multiplier = direction === "asc" ? 1 : -1;
        if (left === null || left === undefined) {
            return 1;
        }
        if (right === null || right === undefined) {
            return -1;
        }
        if (typeof left === "string" || typeof right === "string") {
            return String(left).localeCompare(String(right), "zh-CN") * multiplier;
        }
        return (Number(left) - Number(right)) * multiplier;
    }

    function computeSellRatio(item) {
        const steamSell = Number(item.steam_sell_price);
        const buffSell = Number(item.buff_sell_price);
        if (!steamSell || steamSell <= 0 || !buffSell) {
            return null;
        }
        return buffSell / (steamSell * 0.85);
    }

    function getRatioClass(value) {
        if (value === null) {
            return "warn";
        }
        if (value <= 0.8) {
            return "good";
        }
        if (value < 1.0) {
            return "warn";
        }
        return "bad";
    }

    function formatRatio(value) {
        return value === null ? "--" : value.toFixed(2);
    }

    function formatCurrency(value) {
        const number = Number(value);
        return Number.isFinite(number) && number > 0 ? `¥${number.toFixed(2)}` : "--";
    }

    function renderTable() {
        if (!elements.dataTableBody) {
            return;
        }

        const pageData = getCurrentPageData();
        if (!pageData.length) {
            elements.dataTableBody.innerHTML = `<tr><td colspan="${isAdmin ? 10 : 9}"><div class="empty-state">当前条件下没有可展示的数据。</div></td></tr>`;
            return;
        }

        elements.dataTableBody.innerHTML = pageData.map((item) => {
            const sellRatio = computeSellRatio(item);
            const checked = state.selectedItemIds.has(String(item.buff_id)) ? "checked" : "";
            return `
                <tr>
                    ${isAdmin ? `<td><input type="checkbox" class="row-selector" data-item-id="${escapeHtml(String(item.buff_id))}" ${checked}></td>` : ""}
                    <td>
                        <strong>${escapeHtml(item.name || "--")}</strong>
                        <div class="soft">${escapeHtml(item.kind || "--")}</div>
                    </td>
                    <td>
                        <div class="market-links">
                            <a class="market-link" href="https://buff.163.com/goods/${encodeURIComponent(item.buff_id)}" target="_blank" rel="noreferrer">Buff</a>
                            <a class="market-link" href="https://steamcommunity.com/market/listings/${item.steam_market_id}" target="_blank" rel="noreferrer">Steam</a>
                        </div>
                    </td>
                    <td class="mono">${formatCurrency(item.buff_sell_price)}</td>
                    <td class="mono">${formatCurrency(item.steam_sell_price)}</td>
                    <td><span class="ratio ${getRatioClass(sellRatio)}">${formatRatio(sellRatio)}</span></td>
                    <td class="mono">${formatInteger(item.buff_sell_num)}</td>
                    <td class="mono">${formatInteger(item.steam_sell_num)}</td>
                    <td class="mono">${formatInteger(item.steam_volume)}</td>
                    <td class="mono">${escapeHtml(item.update_time || "--")}</td>
                </tr>
            `;
        }).join("");

        elements.dataTableBody.querySelectorAll(".row-selector").forEach((checkbox) => {
            checkbox.addEventListener("change", () => {
                toggleItemSelection(checkbox.dataset.itemId, checkbox.checked);
            });
        });
        syncSelectAllCheckbox();
        updateSelectionSummary();
    }

    function renderPagination() {
        if (!elements.pagination) {
            return;
        }

        const data = getCurrentData();
        const totalPages = Math.max(1, Math.ceil(data.length / state.itemsPerPage));
        if (state.currentPage > totalPages) {
            state.currentPage = totalPages;
        }

        const controls = [];
        controls.push(`<button class="btn btn-secondary" data-page-nav="${state.currentPage - 1}" ${state.currentPage === 1 ? "disabled" : ""}>上一页</button>`);
        getPageNumbers(state.currentPage, totalPages).forEach((page) => {
            if (page === "...") {
                controls.push('<span class="soft">...</span>');
            } else {
                controls.push(`<button class="btn btn-secondary ${page === state.currentPage ? "is-active" : ""}" data-page-nav="${page}">${page}</button>`);
            }
        });
        controls.push(`<button class="btn btn-secondary" data-page-nav="${state.currentPage + 1}" ${state.currentPage === totalPages ? "disabled" : ""}>下一页</button>`);

        elements.pagination.innerHTML = `
            <div class="pagination-info">第 ${state.currentPage} / ${totalPages} 页，共 ${data.length} 条记录</div>
            <div class="pagination-controls">${controls.join("")}</div>
            <div class="pagination-jump">
                <input class="input" style="width:88px" id="page-jump-input" type="number" min="1" max="${totalPages}" value="${state.currentPage}">
                <button class="btn btn-secondary" id="page-jump-btn">跳转</button>
            </div>
        `;

        elements.pagination.querySelectorAll("[data-page-nav]").forEach((button) => {
            button.addEventListener("click", () => goToPage(Number(button.dataset.pageNav), totalPages));
        });
        elements.pagination.querySelector("#page-jump-btn")?.addEventListener("click", () => {
            const input = elements.pagination.querySelector("#page-jump-input");
            goToPage(Number(input.value), totalPages);
        });
        elements.pagination.querySelector("#page-jump-input")?.addEventListener("keydown", (event) => {
            if (event.key === "Enter") {
                goToPage(Number(event.currentTarget.value), totalPages);
            }
        });
    }

    function goToPage(page, totalPages) {
        state.currentPage = Math.min(Math.max(page, 1), totalPages);
        updateDashboardMetrics();
        renderTable();
        renderPagination();
        syncSelectAllCheckbox();
    }

    function getPageNumbers(current, total) {
        const pages = [];
        if (total <= 7) {
            for (let page = 1; page <= total; page += 1) {
                pages.push(page);
            }
            return pages;
        }
        pages.push(1);
        if (current > 3) {
            pages.push("...");
        }
        const start = Math.max(2, current - 1);
        const end = Math.min(total - 1, current + 1);
        for (let page = start; page <= end; page += 1) {
            pages.push(page);
        }
        if (current < total - 2) {
            pages.push("...");
        }
        pages.push(total);
        return pages;
    }

    function writeLog(container, type, message) {
        if (!container) {
            return;
        }
        const line = document.createElement("p");
        line.className = `log-${type}`;
        line.textContent = `[${new Date().toLocaleTimeString("zh-CN", { hour12: false })}] ${message}`;
        container.prepend(line);
    }

    function logout() {
        if (window.confirm("确定要退出登录吗？")) {
            window.location.href = "/logout";
        }
    }

    function setClearButtonsDisabled(disabled) {
        elements.clearButtons.forEach((button) => {
            button.disabled = disabled;
        });
    }

    function formatInteger(value) {
        const number = Number(value);
        return Number.isFinite(number) ? number.toLocaleString("zh-CN") : "--";
    }

    function parseDateOnly(value) {
        return value ? String(value).slice(0, 10) : null;
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
