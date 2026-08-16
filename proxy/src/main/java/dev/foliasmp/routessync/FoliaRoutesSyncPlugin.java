package dev.foliasmp.routessync;

import com.google.inject.Inject;
import com.velocitypowered.api.event.ResultedEvent.ComponentResult;
import com.velocitypowered.api.event.Subscribe;
import com.velocitypowered.api.event.connection.LoginEvent;
import com.velocitypowered.api.event.player.PlayerChatEvent;
import com.velocitypowered.api.event.player.PlayerChooseInitialServerEvent;
import com.velocitypowered.api.event.proxy.ProxyInitializeEvent;
import com.velocitypowered.api.event.proxy.ProxyPingEvent;
import com.velocitypowered.api.plugin.Plugin;
import com.velocitypowered.api.proxy.Player;
import com.velocitypowered.api.proxy.ProxyServer;
import com.velocitypowered.api.proxy.server.RegisteredServer;
import com.velocitypowered.api.proxy.server.ServerInfo;
import com.velocitypowered.api.proxy.server.ServerPing;
import com.velocitypowered.api.util.Favicon;
import net.kyori.adventure.text.Component;
import net.kyori.adventure.text.minimessage.MiniMessage;
import org.slf4j.Logger;

import java.net.InetSocketAddress;
import java.time.Duration;
import java.util.ArrayList;
import java.util.Collections;
import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.Set;
import java.util.UUID;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.atomic.AtomicReference;

/**
 * Three related jobs, all driven by polling one or more independent
 * folia-nexa-mgmt clusters:
 *
 * <ol>
 *   <li>Keeps Velocity's backend server list in sync with each cluster's
 *       {@code GET /api/v1/routes}, so worlds can come and go without a
 *       proxy restart (PLAN.md §7). Every registered server name is
 *       qualified with its owning cluster's id ({@link ServerName}) so
 *       two clusters' same-named worlds never collide.</li>
 *   <li>Resolves a connecting Java player's virtual host (the hostname
 *       carried in the login handshake — Bedrock/RakNet has no
 *       equivalent) against every cluster's {@code domains} map, merged
 *       by {@link DomainRouteMerger}, to land them on the right
 *       cluster's default world (PLAN.md §7C, §7D). Unmatched
 *       connections — including all Bedrock ones — fall back to the
 *       configured primary cluster's own default.</li>
 *   <li>Optionally gates login entirely against a cluster's
 *       {@code GET /api/v1/access-requests/approved-uuids} — only
 *       Discord-approved players get past the proxy at all (PLAN.md
 *       §11C), checked against whichever cluster the connection's
 *       virtual host resolves to.</li>
 * </ol>
 *
 * <p>Configuration is via environment variables — matching how the rest
 * of this project's snaps are configured (mgmt, node). See
 * {@link ClusterConfig} for the single-cluster vs. multi-cluster env var
 * shapes ({@code FOLIA_MGMT_URL}/{@code FOLIA_MGMT_API_TOKEN} vs.
 * {@code FOLIA_MGMT_CLUSTER_IDS}/{@code FOLIA_MGMT_CLUSTER_<ID>_URL}/
 * {@code FOLIA_MGMT_CLUSTER_<ID>_TOKEN}/{@code FOLIA_MGMT_PRIMARY_CLUSTER}).
 * Two more apply globally, across every configured cluster:
 *
 * <ul>
 *   <li>{@code FOLIA_ROUTES_POLL_SECONDS} (optional, default 5)</li>
 *   <li>{@code FOLIA_ACCESS_GATE_ENABLED} (optional, default {@code false}) — opt-in on purpose, so a fresh install never locks the operator out by surprise</li>
 * </ul>
 */
@Plugin(
        id = "folia-routes-sync",
        name = "Folia Routes Sync",
        version = "0.1.0",
        description = "Syncs Velocity's backend list and login access with one or more folia-nexa-mgmt clusters"
)
public final class FoliaRoutesSyncPlugin {

    /** Everything this plugin tracks for one configured mgmt cluster. */
    private static final class ClusterRuntime {
        final ClusterConfig config;
        final MgmtRoutesClient routesClient;
        final AccessGateClient accessGateClient;
        final MgmtDisplayClient displayClient;
        final ChatClient chatClient;
        // Written only from the poll task's single thread; read from event
        // handler threads too, so each needs to be independently visible —
        // matches the AtomicReference/volatile discipline this class used
        // before it tracked just one cluster.
        volatile Map<String, String> domainRoutes = Map.of();
        volatile String defaultWorld;
        volatile Set<UUID> approvedUuids = Set.of();
        volatile ProxyDisplay display;

        ClusterRuntime(ClusterConfig config, Duration timeout) {
            this.config = config;
            this.routesClient = new MgmtRoutesClient(config.mgmtUrl, config.apiToken, timeout);
            this.accessGateClient = new AccessGateClient(config.mgmtUrl, config.apiToken, timeout);
            this.displayClient = new MgmtDisplayClient(config.mgmtUrl, config.apiToken, timeout);
            this.chatClient = new ChatClient(config.mgmtUrl, config.apiToken, timeout);
        }
    }

    private final ProxyServer server;
    private final Logger logger;
    // Populated once in onProxyInitialize, before the repeating poll task
    // is scheduled or any event can fire — AtomicReference just to be
    // explicit about safe publication across threads, not for any
    // compare-and-swap use.
    // Collections.emptyMap() rather than Map.of() deliberately — the
    // no-arg immutable-map factory rejects a null lookup key, and
    // primaryClusterId (used as a lookup key below) starts out null until
    // onProxyInitialize runs.
    private final AtomicReference<Map<String, ClusterRuntime>> clusters = new AtomicReference<>(Collections.emptyMap());
    private volatile String primaryClusterId;
    private final AtomicReference<Map<String, String>> mergedDomainRoutes = new AtomicReference<>(Collections.emptyMap());
    private final AtomicBoolean accessGateEnabled = new AtomicBoolean(false);

    @Inject
    public FoliaRoutesSyncPlugin(ProxyServer server, Logger logger) {
        this.server = server;
        this.logger = logger;
    }

    @Subscribe
    public void onProxyInitialize(ProxyInitializeEvent event) {
        ClusterConfig.Parsed parsed = ClusterConfig.parse(System.getenv());
        primaryClusterId = parsed.primaryClusterId();
        int pollSeconds = Integer.parseInt(System.getenv().getOrDefault("FOLIA_ROUTES_POLL_SECONDS", "5"));
        accessGateEnabled.set(Boolean.parseBoolean(System.getenv().getOrDefault("FOLIA_ACCESS_GATE_ENABLED", "false")));

        Duration timeout = Duration.ofSeconds(10);
        Map<String, ClusterRuntime> built = new LinkedHashMap<>();
        for (ClusterConfig cc : parsed.clusters()) {
            built.put(cc.id, new ClusterRuntime(cc, timeout));
        }
        clusters.set(built);

        logger.info("polling {} mgmt cluster(s) every {}s (primary: '{}', access gate: {})",
                built.size(), pollSeconds, primaryClusterId, accessGateEnabled.get() ? "enabled" : "disabled");
        for (ClusterRuntime c : built.values()) {
            logger.info("  cluster '{}': {}", c.config.id, c.config.mgmtUrl);
        }

        server.getScheduler()
                .buildTask(this, this::pollAndReconcileAll)
                .repeat(Duration.ofSeconds(pollSeconds))
                .schedule();
    }

    @Subscribe
    public void onPlayerChat(PlayerChatEvent event) {
        // Fire-and-forget on the scheduler's async pool — never block chat
        // delivery on an mgmt round trip. Doesn't touch event.setResult at
        // all: this only ever mirrors chat outward to Discord (PLAN.md
        // §16), never filters or modifies what players actually see.
        Player player = event.getPlayer();
        String qualifiedWorld = player.getCurrentServer().map(conn -> conn.getServerInfo().getName()).orElse(null);
        if (qualifiedWorld == null) {
            return;
        }
        Optional<String> clusterId = ServerName.clusterIdOf(qualifiedWorld);
        Optional<String> worldName = ServerName.worldNameOf(qualifiedWorld);
        if (clusterId.isEmpty() || worldName.isEmpty()) {
            return;
        }
        ClusterRuntime cluster = clusters.get().get(clusterId.get());
        if (cluster == null) {
            return;
        }
        server.getScheduler().buildTask(this, () -> {
            try {
                cluster.chatClient.report(worldName.get(), player.getUsername(), event.getMessage());
            } catch (Exception e) {
                logger.debug("cluster '{}': failed to report chat message to mgmt: {}", cluster.config.id, e.getMessage());
            }
        }).schedule();
    }

    @Subscribe
    public void onProxyPing(ProxyPingEvent event) {
        Optional<String> virtualHost = event.getConnection().getVirtualHost().map(InetSocketAddress::getHostString);
        ClusterRuntime cluster = resolveCluster(virtualHost);
        ProxyDisplay current = cluster == null ? null : cluster.display;
        if (current == null) {
            return;
        }
        ServerPing.Builder builder = event.getPing().asBuilder();
        builder.description(MiniMessage.miniMessage().deserialize(current.motd()));
        if (current.iconBase64() != null) {
            builder.favicon(new Favicon("data:image/png;base64," + current.iconBase64()));
        }
        event.setPing(builder.build());
    }

    @Subscribe
    public void onChooseInitialServer(PlayerChooseInitialServerEvent event) {
        Optional<String> virtualHost = event.getPlayer().getVirtualHost().map(InetSocketAddress::getHostString);
        ClusterRuntime primary = clusters.get().get(primaryClusterId);
        String fallback = primary == null ? null : primary.defaultWorld;
        VirtualHostRouter.resolve(mergedDomainRoutes.get(), virtualHost, fallback)
                .flatMap(server::getServer)
                .ifPresent(event::setInitialServer);
    }

    @Subscribe
    public void onLogin(LoginEvent event) {
        UUID playerId = event.getPlayer().getUniqueId();
        ClusterRuntime cluster = resolveCluster(event.getPlayer().getVirtualHost().map(InetSocketAddress::getHostString));
        // No cluster resolved at all (e.g. a connection arriving in the
        // brief window before onProxyInitialize finishes) — nothing
        // sensible to gate against, so fail open rather than lock every
        // player out on a startup race.
        Set<UUID> approved = cluster == null ? Set.of() : cluster.approvedUuids;
        if (AccessDecision.isAllowed(accessGateEnabled.get() && cluster != null, approved, playerId)) {
            return;
        }
        logger.info("cluster '{}': denied login for {} ({}) — not on the approved list",
                cluster.config.id, event.getPlayer().getUsername(), playerId);
        event.setResult(ComponentResult.denied(
                Component.text("Access not approved yet. Request access via Discord, then try again.")
        ));
    }

    /** Which cluster a connection's virtual host belongs to, falling back to the configured primary. */
    private ClusterRuntime resolveCluster(Optional<String> virtualHost) {
        Map<String, ClusterRuntime> current = clusters.get();
        String qualified = virtualHost.map(VirtualHostRouter::normalize).map(mergedDomainRoutes.get()::get).orElse(null);
        if (qualified != null) {
            ClusterRuntime matched = ServerName.clusterIdOf(qualified).map(current::get).orElse(null);
            if (matched != null) {
                return matched;
            }
        }
        return current.get(primaryClusterId);
    }

    private void pollAndReconcileAll() {
        Map<String, ClusterRuntime> current = clusters.get();
        for (ClusterRuntime cluster : current.values()) {
            reconcileRoutes(cluster);
            if (accessGateEnabled.get()) {
                refreshApprovedUuids(cluster);
            }
            refreshDisplay(cluster);
            deliverPendingChat(cluster);
        }
        recomputeMergedDomainRoutes(current);
    }

    private void recomputeMergedDomainRoutes(Map<String, ClusterRuntime> current) {
        Map<String, Map<String, String>> perCluster = new LinkedHashMap<>();
        for (ClusterRuntime c : current.values()) {
            perCluster.put(c.config.id, c.domainRoutes);
        }
        DomainRouteMerger.Result result = DomainRouteMerger.merge(new ArrayList<>(current.keySet()), perCluster);
        for (String collision : result.collisions()) {
            logger.warn("domain routing collision: {}", collision);
        }
        mergedDomainRoutes.set(result.domainRoutes());
    }

    // A Discord username/message could itself contain MiniMessage tag
    // syntax ("<red>") — escape it so it's shown literally rather than
    // parsed as formatting. Adventure's MiniMessage ships a real escaper
    // for exactly this (deserializing untrusted text); using it here
    // instead of hand-rolled escaping keeps this correct across whatever
    // tag syntax MiniMessage itself supports.
    private static String escapeMiniMessage(String text) {
        return MiniMessage.miniMessage().escapeTags(text);
    }

    private void deliverPendingChat(ClusterRuntime cluster) {
        List<PendingChatMessage> pending;
        try {
            pending = cluster.chatClient.fetchPending();
        } catch (Exception e) {
            logger.debug("cluster '{}': failed to fetch pending chat from mgmt: {}", cluster.config.id, e.getMessage());
            return;
        }
        String prefix = cluster.config.id + "__";
        for (PendingChatMessage msg : pending) {
            Component component = MiniMessage.miniMessage().deserialize(
                    "<gray>[Discord] <bold>" + escapeMiniMessage(msg.author()) + "</bold>: "
                            + escapeMiniMessage(msg.message()) + "</gray>"
            );
            if (msg.world() == null) {
                // Broadcast means "every player on this cluster", not
                // every player on every cluster this proxy happens to
                // also be routing for.
                server.getAllPlayers().stream()
                        .filter(p -> p.getCurrentServer()
                                .map(cs -> cs.getServerInfo().getName().startsWith(prefix))
                                .orElse(false))
                        .forEach(p -> p.sendMessage(component));
            } else {
                server.getServer(ServerName.qualify(cluster.config.id, msg.world()))
                        .ifPresent(rs -> rs.getPlayersConnected().forEach(p -> p.sendMessage(component)));
            }
        }
    }

    private void reconcileRoutes(ClusterRuntime cluster) {
        List<Route> desired;
        Map<String, String> domainRoutes;
        try {
            MgmtRoutesClient.RoutesFetch fetched = cluster.routesClient.fetch();
            domainRoutes = fetched.domainRoutes();
            desired = fetched.routes().stream()
                    .map(r -> new Route(ServerName.qualify(cluster.config.id, r.world()), r.type(), r.host(), r.port(), r.isDefault()))
                    .toList();
        } catch (Exception e) {
            logger.warn("cluster '{}': failed to fetch routes from mgmt, keeping current server list: {}", cluster.config.id, e.getMessage());
            return;
        }
        cluster.domainRoutes = domainRoutes;

        String prefix = cluster.config.id + "__";
        Map<String, String> currentlyRegistered = new HashMap<>();
        for (RegisteredServer registered : server.getAllServers()) {
            ServerInfo info = registered.getServerInfo();
            // Scoped to this cluster's own prior registrations only — an
            // unscoped diff would see every *other* cluster's worlds as
            // "no longer in mgmt's route table" and unregister them.
            if (info.getName().startsWith(prefix)) {
                currentlyRegistered.put(info.getName(), addressOf(info));
            }
        }

        RouteDiff.Plan plan = RouteDiff.compute(currentlyRegistered, desired);

        for (Route route : plan.toRegister()) {
            // Velocity keys registration by name and won't let us just
            // overwrite an existing one in place, so an address change
            // (world moved hosts) needs the stale entry gone first.
            server.getServer(route.world()).ifPresent(existing -> server.unregisterServer(existing.getServerInfo()));
            server.registerServer(new ServerInfo(route.world(), new InetSocketAddress(route.host(), route.port())));
            logger.info("cluster '{}': registered '{}' -> {}", cluster.config.id, route.world(), route.address());
        }

        for (String name : plan.toUnregister()) {
            server.getServer(name).ifPresent(rs -> {
                server.unregisterServer(rs.getServerInfo());
                logger.info("cluster '{}': unregistered '{}' (no longer in mgmt's route table)", cluster.config.id, name);
            });
        }

        // Already qualified (see the .map(...) above) — this cluster's own
        // default server name, ready to use directly as a fallback target.
        plan.defaultWorld().ifPresent(w -> cluster.defaultWorld = w);
    }

    private void refreshApprovedUuids(ClusterRuntime cluster) {
        try {
            Set<UUID> latest = cluster.accessGateClient.fetchApprovedUuids();
            cluster.approvedUuids = latest;
            logger.debug("cluster '{}': access gate: {} approved player(s)", cluster.config.id, latest.size());
        } catch (Exception e) {
            logger.warn("cluster '{}': failed to fetch approved players from mgmt, keeping previous list: {}", cluster.config.id, e.getMessage());
        }
    }

    private void refreshDisplay(ClusterRuntime cluster) {
        try {
            cluster.display = cluster.displayClient.fetch();
        } catch (Exception e) {
            logger.warn("cluster '{}': failed to fetch display config from mgmt, keeping previous motd/icon: {}", cluster.config.id, e.getMessage());
        }
    }

    private static String addressOf(ServerInfo info) {
        InetSocketAddress addr = info.getAddress();
        return addr.getHostString() + ":" + addr.getPort();
    }
}
