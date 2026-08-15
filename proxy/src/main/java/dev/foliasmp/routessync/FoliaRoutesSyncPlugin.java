package dev.foliasmp.routessync;

import com.google.inject.Inject;
import com.velocitypowered.api.event.ResultedEvent.ComponentResult;
import com.velocitypowered.api.event.Subscribe;
import com.velocitypowered.api.event.connection.LoginEvent;
import com.velocitypowered.api.event.player.PlayerChooseInitialServerEvent;
import com.velocitypowered.api.event.proxy.ProxyInitializeEvent;
import com.velocitypowered.api.plugin.Plugin;
import com.velocitypowered.api.proxy.ProxyServer;
import com.velocitypowered.api.proxy.server.RegisteredServer;
import com.velocitypowered.api.proxy.server.ServerInfo;
import net.kyori.adventure.text.Component;
import org.slf4j.Logger;

import java.net.InetSocketAddress;
import java.time.Duration;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.UUID;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.atomic.AtomicReference;

/**
 * Two related jobs, both driven by polling folia-nexa-mgmt:
 *
 * <ol>
 *   <li>Keeps Velocity's backend server list in sync with
 *       {@code GET /api/v1/routes}, so worlds can come and go without a
 *       proxy restart (PLAN.md §7).</li>
 *   <li>Optionally gates login entirely against
 *       {@code GET /api/v1/access-requests/approved-uuids} — only
 *       Discord-approved players get past the proxy at all (PLAN.md
 *       §11C).</li>
 * </ol>
 *
 * Configuration is via environment variables — matching how the rest of
 * this project's snaps are configured (mgmt, node), and avoiding a second
 * config-file format for what's fundamentally a handful of settings:
 *
 * <ul>
 *   <li>{@code FOLIA_MGMT_URL} (required) — e.g. https://mgmt.internal:8443</li>
 *   <li>{@code FOLIA_MGMT_API_TOKEN} (required) — an mgmt API token; viewer role is enough for both endpoints this plugin reads</li>
 *   <li>{@code FOLIA_ROUTES_POLL_SECONDS} (optional, default 5)</li>
 *   <li>{@code FOLIA_ACCESS_GATE_ENABLED} (optional, default {@code false}) — opt-in on purpose, so a fresh install never locks the operator out by surprise</li>
 * </ul>
 */
@Plugin(
        id = "folia-routes-sync",
        name = "Folia Routes Sync",
        version = "0.1.0",
        description = "Syncs Velocity's backend list and login access with folia-nexa-mgmt"
)
public final class FoliaRoutesSyncPlugin {

    private final ProxyServer server;
    private final Logger logger;
    private final AtomicReference<String> defaultWorld = new AtomicReference<>();
    private final AtomicReference<Set<UUID>> approvedUuids = new AtomicReference<>(Set.of());
    private final AtomicBoolean accessGateEnabled = new AtomicBoolean(false);

    @Inject
    public FoliaRoutesSyncPlugin(ProxyServer server, Logger logger) {
        this.server = server;
        this.logger = logger;
    }

    @Subscribe
    public void onProxyInitialize(ProxyInitializeEvent event) {
        String mgmtUrl = requireEnv("FOLIA_MGMT_URL");
        String apiToken = requireEnv("FOLIA_MGMT_API_TOKEN");
        int pollSeconds = Integer.parseInt(System.getenv().getOrDefault("FOLIA_ROUTES_POLL_SECONDS", "5"));
        accessGateEnabled.set(Boolean.parseBoolean(System.getenv().getOrDefault("FOLIA_ACCESS_GATE_ENABLED", "false")));

        MgmtRoutesClient routesClient = new MgmtRoutesClient(mgmtUrl, apiToken, Duration.ofSeconds(10));
        AccessGateClient accessGateClient = new AccessGateClient(mgmtUrl, apiToken, Duration.ofSeconds(10));

        logger.info("polling {} every {}s (access gate: {})", mgmtUrl, pollSeconds, accessGateEnabled.get() ? "enabled" : "disabled");

        server.getScheduler()
                .buildTask(this, () -> pollAndReconcile(routesClient, accessGateClient))
                .repeat(Duration.ofSeconds(pollSeconds))
                .schedule();
    }

    @Subscribe
    public void onChooseInitialServer(PlayerChooseInitialServerEvent event) {
        String world = defaultWorld.get();
        if (world == null) {
            return;
        }
        server.getServer(world).ifPresent(event::setInitialServer);
    }

    @Subscribe
    public void onLogin(LoginEvent event) {
        UUID playerId = event.getPlayer().getUniqueId();
        if (AccessDecision.isAllowed(accessGateEnabled.get(), approvedUuids.get(), playerId)) {
            return;
        }
        logger.info("denied login for {} ({}) — not on the approved list", event.getPlayer().getUsername(), playerId);
        event.setResult(ComponentResult.denied(
                Component.text("Access not approved yet. Request access via Discord, then try again.")
        ));
    }

    private void pollAndReconcile(MgmtRoutesClient routesClient, AccessGateClient accessGateClient) {
        reconcileRoutes(routesClient);
        if (accessGateEnabled.get()) {
            refreshApprovedUuids(accessGateClient);
        }
    }

    private void reconcileRoutes(MgmtRoutesClient client) {
        List<Route> desired;
        try {
            desired = client.fetch();
        } catch (Exception e) {
            logger.warn("failed to fetch routes from mgmt, keeping current server list: {}", e.getMessage());
            return;
        }

        Map<String, String> currentlyRegistered = new HashMap<>();
        for (RegisteredServer registered : server.getAllServers()) {
            ServerInfo info = registered.getServerInfo();
            currentlyRegistered.put(info.getName(), addressOf(info));
        }

        RouteDiff.Plan plan = RouteDiff.compute(currentlyRegistered, desired);

        for (Route route : plan.toRegister()) {
            // Velocity keys registration by name and won't let us just
            // overwrite an existing one in place, so an address change
            // (world moved hosts) needs the stale entry gone first.
            server.getServer(route.world()).ifPresent(existing -> server.unregisterServer(existing.getServerInfo()));
            server.registerServer(new ServerInfo(route.world(), new InetSocketAddress(route.host(), route.port())));
            logger.info("registered '{}' -> {}", route.world(), route.address());
        }

        for (String name : plan.toUnregister()) {
            server.getServer(name).ifPresent(rs -> {
                server.unregisterServer(rs.getServerInfo());
                logger.info("unregistered '{}' (no longer in mgmt's route table)", name);
            });
        }

        plan.defaultWorld().ifPresent(defaultWorld::set);
    }

    private void refreshApprovedUuids(AccessGateClient client) {
        try {
            Set<UUID> latest = client.fetchApprovedUuids();
            approvedUuids.set(latest);
            logger.debug("access gate: {} approved player(s)", latest.size());
        } catch (Exception e) {
            logger.warn("failed to fetch approved players from mgmt, keeping previous list: {}", e.getMessage());
        }
    }

    private static String addressOf(ServerInfo info) {
        InetSocketAddress addr = info.getAddress();
        return addr.getHostString() + ":" + addr.getPort();
    }

    private static String requireEnv(String name) {
        String value = System.getenv(name);
        if (value == null || value.isBlank()) {
            throw new IllegalStateException(name + " must be set (see snapcraft.yaml environment)");
        }
        return value;
    }
}
