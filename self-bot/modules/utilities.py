def get_command_info(bot):
    return "\n".join(f"{cmd.name} - {cmd.help}" for cmd in bot.commands)
