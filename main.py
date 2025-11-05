import discord
from discord import app_commands
from discord.ext import commands, tasks
import os
from datetime import datetime, timedelta

# -------------------------
# CONFIGURAÇÃO BÁSICA
# -------------------------
intents = discord.Intents.default()
intents.members = True
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)
GUILD_ID = 1420347024376725526  # id do servidor

# -------------------------
# VARIÁVEIS GLOBAIS
# -------------------------
invites_cache = {}
bots_permitidos = []
antilink_ativo = True
mutes = {}

# -------------------------
# FUNÇÕES AUXILIARES
# -------------------------
def tem_cargo_soberba(member: discord.Member) -> bool:
    return any(r.name.lower() == "soberba" for r in member.roles)

async def ensure_muted_role(guild: discord.Guild):
    role = discord.utils.get(guild.roles, name="mutado")
    if not role:
        role = await guild.create_role(name="mutado", reason="cargo criado para mutes")
        for canal in guild.channels:
            await canal.set_permissions(role, send_messages=False, speak=False)
    return role

async def update_invites_cache():
    for guild in bot.guilds:
        try:
            invites_cache[guild.id] = {invite.code: invite for invite in await guild.invites()}
        except discord.Forbidden:
            print(f"🚫 sem permissão para ver convites no servidor {guild.name}")
    print("✅ cache de convites atualizado.")

# -------------------------
# EVENTOS
# -------------------------
@bot.event
async def on_ready():
    print(f"✅ {bot.user} está online e pronto!")
    try:
        guild = discord.Object(id=GUILD_ID)
        synced = await bot.tree.sync(guild=guild)
        print(f"✅ {len(synced)} comandos sincronizados com o servidor {GUILD_ID}.")
    except Exception as e:
        print(f"erro ao sincronizar comandos: {e}")

    verificar_mutes.start()
    await update_invites_cache()

@bot.event
async def on_message(message):
    global antilink_ativo
    if message.author.bot:
        return
    if antilink_ativo and ("http://" in message.content or "https://" in message.content):
        await message.delete()
        embed = discord.Embed(
            description=f"🚫 {message.author.mention}, links não são permitidos!",
            color=discord.Color.red()
        )
        await message.channel.send(embed=embed, delete_after=5)
    await bot.process_commands(message)

# -------------------------
# LOOP DE VERIFICAÇÃO DE MUTES
# -------------------------
@tasks.loop(seconds=30)
async def verificar_mutes():
    agora = datetime.utcnow()
    expirados = [user_id for user_id, fim in mutes.items() if agora >= fim]
    for user_id in expirados:
        for guild in bot.guilds:
            member = guild.get_member(user_id)
            if member:
                role = discord.utils.get(guild.roles, name="mutado")
                if role in member.roles:
                    try:
                        await member.remove_roles(role)
                        print(f"🔊 {member} foi desmutado automaticamente.")
                    except Exception:
                        pass
        del mutes[user_id]

# -------------------------
# COMANDOS SLASH
# -------------------------

@bot.tree.command(name="sync", description="sincroniza os comandos (somente soberba).")
async def sync(interaction: discord.Interaction):
    if not tem_cargo_soberba(interaction.user):
        await interaction.response.send_message("🚫 permissão negada (soberba necessária).", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    try:
        guild = discord.Object(id=GUILD_ID)
        synced = await bot.tree.sync(guild=guild)
        await interaction.followup.send(f"✅ {len(synced)} comandos sincronizados com sucesso.", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ erro ao sincronizar: {e}", ephemeral=True)

@bot.tree.command(name="menu_admin", description="mostra o menu de comandos administrativos (somente soberba).")
async def menu_admin(interaction: discord.Interaction):
    if not tem_cargo_soberba(interaction.user):
        await interaction.response.send_message("🚫 você não tem permissão para ver este menu.", ephemeral=True)
        return

    texto = """
📜 **comandos administrativos disponíveis:**

🧹 `/clear <quantidade>` → apaga mensagens no canal  
🔨 `/ban <usuários>` → bane até 5 usuários  
🔇 `/mute <tempo> <usuários>` → muta usuários por x minutos  
🚫 `/link <on|off>` → ativa ou desativa o antilink  
💬 `/falar <mensagem>` → faz o bot enviar mensagem  
📊 `/convidados` → mostra convites do servidor  
🔄 `/sync` → força sincronização dos comandos
"""
    embed = discord.Embed(title="👑 menu administrativo", description=texto, color=discord.Color.gold())
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="clear", description="apaga mensagens no canal (somente soberba).")
@app_commands.describe(quantidade="quantidade de mensagens a apagar")
async def clear(interaction: discord.Interaction, quantidade: int):
    if not tem_cargo_soberba(interaction.user):
        await interaction.response.send_message("🚫 permissão negada.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    deleted = await interaction.channel.purge(limit=quantidade)
    embed = discord.Embed(title="🧹 limpeza concluída", description=f"{len(deleted)} mensagens apagadas.", color=discord.Color.dark_gray())
    await interaction.followup.send(embed=embed, ephemeral=True)

@bot.tree.command(name="ban", description="bane até 5 usuários (somente soberba).")
@app_commands.describe(usuario1="usuário 1", usuario2="usuário 2", usuario3="usuário 3", usuario4="usuário 4", usuario5="usuário 5")
async def ban(interaction: discord.Interaction, usuario1: discord.Member, usuario2: discord.Member = None, usuario3: discord.Member = None, usuario4: discord.Member = None, usuario5: discord.Member = None):
    if not tem_cargo_soberba(interaction.user):
        await interaction.response.send_message("🚫 permissão negada.", ephemeral=True)
        return
    usuarios = [u for u in (usuario1, usuario2, usuario3, usuario4, usuario5) if u]
    nomes = []
    for user in usuarios:
        try:
            await interaction.guild.ban(user, reason=f"banido por {interaction.user}")
            nomes.append(user.name)
        except Exception:
            pass
    embed = discord.Embed(title="🔨 banimento", description=f"{', '.join(nomes)} foram banidos.", color=discord.Color.red())
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="mute", description="muta usuários por x minutos (somente soberba).")
@app_commands.describe(tempo="tempo em minutos", usuario1="usuário 1", usuario2="usuário 2", usuario3="usuário 3", usuario4="usuário 4", usuario5="usuário 5")
async def mute(interaction: discord.Interaction, tempo: int, usuario1: discord.Member, usuario2: discord.Member = None, usuario3: discord.Member = None, usuario4: discord.Member = None, usuario5: discord.Member = None):
    if not tem_cargo_soberba(interaction.user):
        await interaction.response.send_message("🚫 permissão negada.", ephemeral=True)
        return
    role = await ensure_muted_role(interaction.guild)
    usuarios = [u for u in (usuario1, usuario2, usuario3, usuario4, usuario5) if u]
    nomes = []
    fim = datetime.utcnow() + timedelta(minutes=tempo)
    for user in usuarios:
        try:
            await user.add_roles(role)
            mutes[user.id] = fim
            nomes.append(user.name)
        except Exception:
            pass
    embed = discord.Embed(title="🔇 usuários mutados", description=f"{', '.join(nomes)} foram mutados por {tempo} minutos.", color=discord.Color.purple())
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="link", description="ativa ou desativa o antilink (somente soberba).")
@app_commands.describe(estado="on ou off")
async def link(interaction: discord.Interaction, estado: str):
    global antilink_ativo
    if not tem_cargo_soberba(interaction.user):
        await interaction.response.send_message("🚫 permissão negada.", ephemeral=True)
        return
    if estado.lower() == "on":
        antilink_ativo = True
        embed = discord.Embed(title="🚫 antilink ativado", color=discord.Color.red())
    elif estado.lower() == "off":
        antilink_ativo = False
        embed = discord.Embed(title="✅ antilink desativado", color=discord.Color.green())
    else:
        await interaction.response.send_message("use `on` ou `off`.", ephemeral=True)
        return
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="falar", description="faz o bot enviar uma mensagem (somente soberba).")
@app_commands.describe(mensagem="mensagem a ser enviada")
async def falar(interaction: discord.Interaction, mensagem: str):
    if not tem_cargo_soberba(interaction.user):
        await interaction.response.send_message("🚫 permissão negada.", ephemeral=True)
        return
    await interaction.response.send_message("✅ mensagem enviada.", ephemeral=True)
    await interaction.channel.send(mensagem)

@bot.tree.command(name="convidados", description="mostra o número de convites do servidor (total ou por usuário).")
@app_commands.describe(usuario="opcional: mencione um usuário para ver quantos ele convidou.")
async def convidados(interaction: discord.Interaction, usuario: discord.Member = None):
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild
    try:
        invites = await guild.invites()
    except discord.Forbidden:
        await interaction.followup.send("🚫 não tenho permissão para ver os convites. ative 'gerenciar convites' pro bot.", ephemeral=True)
        return
    if usuario:
        total_convites = sum(invite.uses for invite in invites if invite.inviter and invite.inviter.id == usuario.id)
        embed = discord.Embed(title="👥 convites de usuário", description=f"{usuario.mention} convidou **{total_convites}** pessoas.", color=discord.Color.blue())
    else:
        inviter_counts = {}
        total_convites = 0
        for invite in invites:
            if invite.inviter:
                inviter_counts[invite.inviter.id] = inviter_counts.get(invite.inviter.id, 0) + invite.uses
                total_convites += invite.uses
        top_inviters = sorted(inviter_counts.items(), key=lambda x: x[1], reverse=True)[:5]
        description = f"**total de convites:** {total_convites}\n\n"
        if top_inviters:
            description += "**top 5:**\n"
            for inviter_id, count in top_inviters:
                inviter = guild.get_member(inviter_id)
                if inviter:
                    description += f"• {inviter.mention}: **{count}** convites\n"
                else:
                    user = bot.get_user(inviter_id)
                    name = user.name if user else f"id {inviter_id}"
                    description += f"• {name}: **{count}** convites\n"
        else:
            description += "nenhum convite registrado."
        embed = discord.Embed(title="📊 estatísticas de convites", description=description, color=discord.Color.green())
    await interaction.followup.send(embed=embed)

# -------------------------
# EXECUÇÃO
# -------------------------
if __name__ == "__main__":
    token = os.getenv("TOKEN")
    if not token:
        print("❌ erro: variável TOKEN não encontrada.")
    else:
        bot.run(token)
