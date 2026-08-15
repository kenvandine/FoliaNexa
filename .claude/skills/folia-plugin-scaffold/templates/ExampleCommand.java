package __PACKAGE__;

import org.bukkit.command.Command;
import org.bukkit.command.CommandExecutor;
import org.bukkit.command.CommandSender;

public final class __COMMAND_CLASS__ implements CommandExecutor {
    private final __MAIN_CLASS_SIMPLE__ plugin;

    public __COMMAND_CLASS__(__MAIN_CLASS_SIMPLE__ plugin) {
        this.plugin = plugin;
    }

    @Override
    public boolean onCommand(CommandSender sender, Command command, String label, String[] args) {
        if (args.length > 0 && args[0].equals("reload") && sender.hasPermission("__PLUGIN_ID_LOWER__.reload")) {
            plugin.reloadConfig();
            sender.sendMessage("Reloaded.");
            return true;
        }
        // TODO: replace with __PLUGIN_NAME__'s actual command behavior.
        sender.sendMessage("Usage: /__COMMAND_NAME__ [reload]");
        return true;
    }
}
