package __PACKAGE__;

import org.bukkit.plugin.java.JavaPlugin;

/**
 * __ONE_LINE_DESCRIPTION__
 *
 * Keep this class thin — wiring only. Actual behavior belongs in plain,
 * independently-testable classes (no org.bukkit.* imports) called from
 * the listener/command classes below. See docs/plugin-dev/02-plugin-
 * architecture.md §2.6 in the folia-server repo for why.
 */
public final class __MAIN_CLASS_SIMPLE__ extends JavaPlugin {

    @Override
    public void onEnable() {
        saveDefaultConfig();
        getServer().getPluginManager().registerEvents(new __LISTENER_CLASS__(this), this);
        getCommand("__COMMAND_NAME__").setExecutor(new __COMMAND_CLASS__(this));
    }

    @Override
    public void onDisable() {
        // Cancel any of your own outstanding scheduled tasks here.
        // Paper cancels tasks scheduled through its own schedulers
        // (Bukkit.getRegionScheduler()/getGlobalRegionScheduler()/
        // getAsyncScheduler()) automatically on plugin disable — this is
        // only needed for anything you're tracking yourself (e.g. a raw
        // ExecutorService).
    }
}
