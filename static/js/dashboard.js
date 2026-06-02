(function () {
    const state = {
        itemsPerPage: 20,
        allData: [],
        filteredData: [],
        currentPage: 1,
        sortKey: "update_time",
        sortDirection: "desc",
        filters: {},
        dataInterval: null,
        statusInterval: null,
        isLoading: false,
        lastDataHash: "",
        crawlerStatus: "stopped",
    };

    const root = document.querySelector("[data-dashboard-root]");
    if (!root) {
        return;
    }

    const isAdmin = root.dataset.isAdmin === "true";

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
        runFilterBtn: document.getElementById("run-filters-btn"),
        resetFilterBtn: document.getElementById("reset-filters-btn"),
        filterToggleBtn: document.getElementById("filter-toggle-btn"),
        filtersBlock: document.getElementById("filters-block"),
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
    };

    const configFieldIds = [
        "proxy-url",
        "buff-session",
        "steam-cookies",
        "max-threads",
        "min-sell-count",
        "min-sell-price",
        "max-sell-price",
        "web-auth",
    ];

    const filterFieldIds = [
        "filter-name",
        "filter-buff-price-min",
        "filter-buff-price-max",
        "filter-steam-price-min",
        "filter-steam-price-max",
        "filter-buy-ratio-min",
        "filter-buy-ratio-max",
        "filter-sell-ratio-min",
        "filter-sell-ratio-max",
        "filter-volume-min",
        "filter-volume-max",
    ];

    document.addEventListener("DOMContentLoaded", init);

    function init() {
        bindPageTabs();
        bindButtons();
        bindSorting();
        setupAutoSave();
        loadConfig();

        if (!isAdmin) {
            activatePage("data");
        } else {
            activatePage("overview");
        }

        checkCrawlerStatus();
        state.statusInterval = window.setInterval(checkCrawlerStatus, 10000);
        fetchData();
    }

    function bindPageTabs() {
        elements.pageTabs.forEach((link) => {
            link.addEventListener("click", (event) => {
                event.preventDefault();
                activatePage(link.dataset.pageTarget);
            });
        });
    }

    function activatePage(pageName) {
        elements.pageTabs.forEach((tab) => {
            tab.classList.toggle("is-active", tab.dataset.pageTarget === pageName);
        });

        elements.pages.forEach((page) => {
            page.hidden = page.dataset.page !== pageName;
        });
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
            elements.saveConfigBtn.addEventListener("click", () => {
                saveConfig(getFormConfig(), true);
            });
        }
        if (elements.testConnectionBtn) {
            elements.testConnectionBtn.addEventListener("click", testConnection);
        }
        if (elements.runFilterBtn) {
            elements.runFilterBtn.addEventListener("click", applyFilters);
        }
        if (elements.resetFilterBtn) {
            elements.resetFilterBtn.addEventListener("click", resetFilters);
        }
        if (elements.filterToggleBtn) {
            elements.filterToggleBtn.addEventListener("click", toggleFilters);
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
                sortCurrentData();
                renderTable();
                renderPagination();
            });
        });
    }

    function setupAutoSave() {
        configFieldIds.forEach((id) => {
            const element = document.getElementById(id);
            if (!element) {
                return;
            }
            const handler = () => saveConfig(getFormConfig(), false);
            element.addEventListener(element.type === "checkbox" ? "change" : "input", handler);
        });

        filterFieldIds.forEach((id) => {
            const element = document.getElementById(id);
            if (!element) {
                return;
            }
            element.addEventListener("keydown", (event) => {
                if (event.key === "Enter") {
                    event.preventDefault();
                    applyFilters();
                }
            });
        });
    }

    function getStorageKey() {
        return "steam_market_dashboard_config_v2";
    }

    function getFormConfig() {
        return {
            proxy_url: document.getElementById("proxy-url")?.value.trim() || "",
            buff_session: document.getElementById("buff-session")?.value.trim() || "",
            steam_cookies: document.getElementById("steam-cookies")?.value.trim() || "",
            max_threads: document.getElementById("max-threads")?.value || "2",
            game_type: document.querySelector('input[name="game_type"]:checked')?.value || "CSGO",
            min_sell_price: document.getElementById("min-sell-price")?.value || "25",
            max_sell_price: document.getElementById("max-sell-price")?.value || "300",
            min_sell_count: document.getElementById("min-sell-count")?.value || "20",
            web_auth: document.getElementById("web-auth")?.checked || false,
        };
    }

    function saveConfig(config, shouldToast) {
        window.localStorage.setItem(getStorageKey(), JSON.stringify(config));
        if (shouldToast) {
            writeLog(elements.crawlerLog, "success", "配置已保存到本地浏览器。");
        }
        updateOverviewCards();
    }

    function loadConfig() {
        const raw = window.localStorage.getItem(getStorageKey());
        if (!raw) {
            updateOverviewCards();
            return;
        }

        try {
            const config = JSON.parse(raw);
            applyConfigToForm(config);
            updateOverviewCards();
        } catch (error) {
            console.error("Failed to parse saved config", error);
        }
    }

    function applyConfigToForm(config) {
        const mapping = {
            "proxy-url": config.proxy_url,
            "buff-session": config.buff_session,
            "steam-cookies": config.steam_cookies,
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

    function updateOverviewCards() {
        const config = getFormConfig();
        if (elements.priceWindow) {
            elements.priceWindow.textContent = `¥${config.min_sell_price} - ¥${config.max_sell_price}`;
        }
        if (elements.activityWindow) {
            elements.activityWindow.textContent = `${config.min_sell_count}+ 在售`;
        }
    }

    function toggleFilters() {
        const collapsed = elements.filtersBlock.hidden;
        elements.filtersBlock.hidden = !collapsed;
        elements.filterToggleBtn.textContent = collapsed ? "收起筛选" : "展开筛选";
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
        const json = await response.json();
        return { response, json };
    }

    async function testConnection() {
        const config = getFormConfig();
        writeLog(elements.testLog, "info", "开始检测 Steam 与 Buff 连通性...");
        elements.testConnectionBtn.disabled = true;

        try {
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

            const steamStatus = json.steam?.success ? "success" : "error";
            const buffStatus = json.buff?.success ? "success" : "error";
            writeLog(elements.testLog, steamStatus, `Steam: ${json.steam?.message || "无返回"}`);
            writeLog(elements.testLog, buffStatus, `Buff: ${json.buff?.message || "无返回"}`);
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
        saveConfig(config, false);
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
                state.filteredData = [];
                state.currentPage = 1;
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

            const nextHash = `${payload.data.length}:${payload.data[0]?.update_time || "none"}:${payload.data[payload.data.length - 1]?.buff_id || "none"}`;
            state.allData = payload.data;
            if (nextHash !== state.lastDataHash) {
                state.lastDataHash = nextHash;
                applyFilters(false);
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

    function updateDashboardMetrics() {
        const data = getCurrentData();
        const totalCount = data.length;
        const candidateCount = data.filter((item) => computeSellRatio(item) !== null && computeSellRatio(item) < 0.88).length;
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
                ? `检测到 ${candidateCount} 条低于 0.88 的潜在机会。`
                : "暂未发现满足当前标准的潜在机会。";
        }
        if (elements.filterSummary) {
            const activeCount = Object.values(state.filters).filter((value) => value !== null && value !== "").length;
            elements.filterSummary.textContent = activeCount ? `已启用 ${activeCount} 项筛选条件。` : "当前未启用任何筛选条件。";
        }
    }

    function getCurrentData() {
        return hasActiveFilters() ? state.filteredData : state.allData;
    }

    function getCurrentPageData() {
        const data = getCurrentData();
        const start = (state.currentPage - 1) * state.itemsPerPage;
        return data.slice(start, start + state.itemsPerPage);
    }

    function hasActiveFilters() {
        return Object.values(state.filters).some((value) => value !== null && value !== "");
    }

    function applyFilters(shouldRender = true) {
        state.filters = {
            name: getInputValue("filter-name").toLowerCase(),
            buffPriceMin: getNumberValue("filter-buff-price-min"),
            buffPriceMax: getNumberValue("filter-buff-price-max"),
            steamPriceMin: getNumberValue("filter-steam-price-min"),
            steamPriceMax: getNumberValue("filter-steam-price-max"),
            buyRatioMin: getNumberValue("filter-buy-ratio-min"),
            buyRatioMax: getNumberValue("filter-buy-ratio-max"),
            sellRatioMin: getNumberValue("filter-sell-ratio-min"),
            sellRatioMax: getNumberValue("filter-sell-ratio-max"),
            volumeMin: getIntegerValue("filter-volume-min"),
            volumeMax: getIntegerValue("filter-volume-max"),
        };

        state.filteredData = state.allData.filter((item) => {
            if (state.filters.name && !String(item.name || "").toLowerCase().includes(state.filters.name)) {
                return false;
            }
            if (state.filters.buffPriceMin !== null && Number(item.buff_sell_price) < state.filters.buffPriceMin) {
                return false;
            }
            if (state.filters.buffPriceMax !== null && Number(item.buff_sell_price) > state.filters.buffPriceMax) {
                return false;
            }
            if (state.filters.steamPriceMin !== null && Number(item.steam_sell_price) < state.filters.steamPriceMin) {
                return false;
            }
            if (state.filters.steamPriceMax !== null && Number(item.steam_sell_price) > state.filters.steamPriceMax) {
                return false;
            }

            const buyRatio = computeBuyRatio(item);
            const sellRatio = computeSellRatio(item);

            if (state.filters.buyRatioMin !== null && (buyRatio === null || buyRatio < state.filters.buyRatioMin)) {
                return false;
            }
            if (state.filters.buyRatioMax !== null && (buyRatio === null || buyRatio > state.filters.buyRatioMax)) {
                return false;
            }
            if (state.filters.sellRatioMin !== null && (sellRatio === null || sellRatio < state.filters.sellRatioMin)) {
                return false;
            }
            if (state.filters.sellRatioMax !== null && (sellRatio === null || sellRatio > state.filters.sellRatioMax)) {
                return false;
            }
            if (state.filters.volumeMin !== null && Number(item.steam_volume) < state.filters.volumeMin) {
                return false;
            }
            if (state.filters.volumeMax !== null && Number(item.steam_volume) > state.filters.volumeMax) {
                return false;
            }
            return true;
        });

        state.currentPage = 1;
        sortCurrentData();
        if (shouldRender) {
            updateDashboardMetrics();
            renderTable();
            renderPagination();
        }
    }

    function resetFilters() {
        filterFieldIds.forEach((id) => {
            const input = document.getElementById(id);
            if (input) {
                input.value = "";
            }
        });
        state.filters = {};
        state.filteredData = [];
        state.currentPage = 1;
        sortCurrentData();
        updateDashboardMetrics();
        renderTable();
        renderPagination();
    }

    function getInputValue(id) {
        return document.getElementById(id)?.value.trim() || "";
    }

    function getNumberValue(id) {
        const raw = getInputValue(id);
        return raw === "" ? null : Number(raw);
    }

    function getIntegerValue(id) {
        const raw = getInputValue(id);
        return raw === "" ? null : parseInt(raw, 10);
    }

    function sortCurrentData() {
        const data = getCurrentData();
        data.sort((left, right) => compareValues(getSortValue(left, state.sortKey), getSortValue(right, state.sortKey), state.sortDirection));
    }

    function getSortValue(item, key) {
        if (key === "steam_buy_ratio") {
            return computeBuyRatio(item);
        }
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

    function computeBuyRatio(item) {
        const steamBuy = Number(item.steam_buy_price);
        const buffSell = Number(item.buff_sell_price);
        if (!steamBuy || steamBuy <= 0 || !buffSell) {
            return null;
        }
        return buffSell / (steamBuy * 0.85);
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
        if (value < 0.88) {
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
        const pageData = getCurrentPageData();
        if (!elements.dataTableBody) {
            return;
        }

        if (!pageData.length) {
            elements.dataTableBody.innerHTML = '<tr><td colspan="10"><div class="empty-state">当前条件下没有可展示的数据。</div></td></tr>';
            return;
        }

        elements.dataTableBody.innerHTML = pageData.map((item) => {
            const buyRatio = computeBuyRatio(item);
            const sellRatio = computeSellRatio(item);
            return `
                <tr>
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
                    <td><span class="ratio ${getRatioClass(buyRatio)}">${formatRatio(buyRatio)}</span></td>
                    <td><span class="ratio ${getRatioClass(sellRatio)}">${formatRatio(sellRatio)}</span></td>
                    <td class="mono">${formatInteger(item.buff_sell_num)}</td>
                    <td class="mono">${formatInteger(item.steam_sell_num)}</td>
                    <td class="mono">${formatInteger(item.steam_volume)}</td>
                    <td class="mono">${escapeHtml(item.update_time || "--")}</td>
                </tr>
            `;
        }).join("");
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

        const info = `<div class="pagination-info">第 ${state.currentPage} / ${totalPages} 页，共 ${data.length} 条记录</div>`;

        const controls = [];
        controls.push(`<button class="btn btn-secondary" data-page-nav="${state.currentPage - 1}" ${state.currentPage === 1 ? "disabled" : ""}>上一页</button>`);
        getPageNumbers(state.currentPage, totalPages).forEach((page) => {
            if (page === "...") {
                controls.push(`<span class="soft">...</span>`);
            } else {
                controls.push(`<button class="btn btn-secondary ${page === state.currentPage ? "is-active" : ""}" data-page-nav="${page}">${page}</button>`);
            }
        });
        controls.push(`<button class="btn btn-secondary" data-page-nav="${state.currentPage + 1}" ${state.currentPage === totalPages ? "disabled" : ""}>下一页</button>`);

        elements.pagination.innerHTML = `
            ${info}
            <div class="pagination-controls">${controls.join("")}</div>
            <div class="pagination-jump">
                <input class="input" style="width:88px" id="page-jump-input" type="number" min="1" max="${totalPages}" value="${state.currentPage}">
                <button class="btn btn-secondary" id="page-jump-btn">跳转</button>
            </div>
        `;

        elements.pagination.querySelectorAll("[data-page-nav]").forEach((button) => {
            button.addEventListener("click", () => {
                const nextPage = Number(button.dataset.pageNav);
                goToPage(nextPage, totalPages);
            });
        });

        elements.pagination.querySelector("#page-jump-btn")?.addEventListener("click", () => {
            const input = elements.pagination.querySelector("#page-jump-input");
            goToPage(Number(input.value), totalPages);
        });

        elements.pagination.querySelector("#page-jump-input")?.addEventListener("keydown", (event) => {
            if (event.key === "Enter") {
                const input = event.currentTarget;
                goToPage(Number(input.value), totalPages);
            }
        });
    }

    function goToPage(page, totalPages) {
        const bounded = Math.min(Math.max(page, 1), totalPages);
        state.currentPage = bounded;
        updateDashboardMetrics();
        renderTable();
        renderPagination();
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

    function escapeHtml(value) {
        return String(value)
            .replaceAll("&", "&amp;")
            .replaceAll("<", "&lt;")
            .replaceAll(">", "&gt;")
            .replaceAll('"', "&quot;")
            .replaceAll("'", "&#39;");
    }
})();
