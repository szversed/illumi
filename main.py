import discord
from discord import app_commands
from discord.ext import commands, tasks
import os
from datetime import datetime, timedelta

intents = discord.Intents.default()
intents.members = True
intents.message_content = True

GUILD_ID = 1420347024376725526

bot = commands.Bot(command_prefix="!", intents=intents)

antilink_ativo = True
mutes = {}
convites_anteriores = {}
convidados = {}

# -------------------------
# funções
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

async def atualizar_convites(guild):
    convites = await guild.invites()
    convites_anteriores[guild.id] = {convite.code: convite.uses for convite in convites}

async def encontrar_convite_usado(guild):
    convites_novos = await guild.invites()
    convites_velhos = convites_anteriores.get(guild.id, {})
    for convite in convites_novos:
        if convite.uses > convites_velhos.get(convite.code, 0):
            convites_anteriores[guild.id] = {c.code: c.uses for c in convites_novos}
            return convite
    convites_anteriores[guild.id] = {c.code: c.uses for c in convites_novos}
    return None

# -------------------------
# eventos
# -------------------------
@bot.event
async def on_ready():
    print(f"✅ {bot.user} está online!")

    guild = discord.Object(id=GUILD_ID)

    # remove comandos globais
    try:
        await bot.tree.clear_commands()
        await bot.tree.sync()
        print("🗑️ comandos globais removidos.")
    except Exception as e:
        print(f"erro ao limpar comandos globais: {e}")

    # sincroniza só comandos da guild
    if not hasattr(bot, "synced"):
        try:
            await bot.tree.sync(guild=guild)
            bot.synced = True
            print("✅ comandos sincronizados apenas na guild.")
        except Exception as e:
            print(f"erro ao sincronizar comandos: {e}")

    for g in bot.guilds:
        await atualizar_convites(g)

    if not verificar_mutes.is_running():
        verificar_mutes.start()
        print("🔁 loop de mutes iniciado.")

@bot.event
async def on_member_join(member):
    convite_usado = await encontrar_convite_usado(member.guild)
    if convite_usado:
        convidador = convite_usado.inviter
        convidados.setdefault(convidador.id, []).append(member.id)

@bot.event
async def on_member_remove(member):
    for convidador_id, lista in convidados.items():
        if member.id in lista:
            lista.remove(member.id)
            break

@bot.event
async def on_message(message):
    global antilink_ativo
    if message.author.bot:
        return
    if antilink_ativo and ("http://" in message.content or "https://" in message.content):
        await message.delete()
        embed = discord.Embed(description=f"🚫 {message.author.mention}, links não são permitidos!", color=discord.Color.red())
        await message.channel.send(embed=embed, delete_after=5)
    await bot.process_commands(message)

# -------------------------
# loop de mutes
# -------------------------
@tasks.loop(seconds=30)
async def verificar_mutes():
    agora = datetime.utcnow()
    expirados = [uid for uid, fim in mutes.items() if agora >= fim]
    for uid in expirados:
        for guild in bot.guilds:
            member = guild.get_member(uid)
            if member:
                role = discord.utils.get(guild.roles, name="mutado")
                if role and role in member.roles:
                    try:
                        await member.remove_roles(role)
                    except Exception:
                        pass
        del mutes[uid]

# -------------------------
# comandos slash
# -------------------------
@bot.tree.command(name="menu_admin", description="menu de comandos administrativos.", guild=discord.Object(id=GUILD_ID))
async def menu_admin(interaction: discord.Interaction):
    if not tem_cargo_soberba(interaction.user):
        await interaction.response.send_message("🚫 sem permissão.", ephemeral=True)
        return
    texto = """
📜 **comandos administrativos:**

🧹 `/clear <quantidade>`  
🔨 `/ban <usuário>`  
🔇 `/mute <tempo> <usuário>`  
🚫 `/link <on|off>`  
💬 `/falar <mensagem>`  
👥 `/convidados <usuário>`
"""
    embed = discord.Embed(title="👑 menu administrativo", description=texto, color=discord.Color.gold())
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="clear", description="apaga mensagens.", guild=discord.Object(id=GUILD_ID))
async def clear(interaction: discord.Interaction, quantidade: int):
    if not tem_cargo_soberba(interaction.user):
        await interaction.response.send_message("🚫 sem permissão.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    deleted = await interaction.channel.purge(limit=quantidade)
    embed = discord.Embed(title="🧹 limpeza concluída", description=f"{len(deleted)} mensagens apagadas.", color=discord.Color.dark_gray())
    await interaction.followup.send(embed=embed, ephemeral=True)

@bot.tree.command(name="ban", description="bane usuário.", guild=discord.Object(id=GUILD_ID))
async def ban(interaction: discord.Interaction, usuario: discord.Member):
    if not tem_cargo_soberba(interaction.user):
        await interaction.response.send_message("🚫 sem permissão.", ephemeral=True)
        return
    try:
        await interaction.guild.ban(usuario, reason=f"banido por {interaction.user}")
        embed = discord.Embed(title="🔨 banido", description=f"{usuario.mention} foi banido.", color=discord.Color.red())
        await interaction.response.send_message(embed=embed)
    except Exception:
        await interaction.response.send_message("erro ao banir.", ephemeral=True)

@bot.tree.command(name="mute", description="muta usuário por x minutos.", guild=discord.Object(id=GUILD_ID))
async def mute(interaction: discord.Interaction, tempo: int, usuario: discord.Member):
    if not tem_cargo_soberba(interaction.user):
        await interaction.response.send_message("🚫 sem permissão.", ephemeral=True)
        return
    role = await ensure_muted_role(interaction.guild)
    fim = datetime.utcnow() + timedelta(minutes=tempo)
    try:
        await usuario.add_roles(role)
        mutes[usuario.id] = fim
        embed = discord.Embed(title="🔇 usuário mutado", description=f"{usuario.mention} mutado por {tempo} minutos.", color=discord.Color.purple())
        await interaction.response.send_message(embed=embed)
    except Exception:
        await interaction.response.send_message("erro ao mutar.", ephemeral=True)

@bot.tree.command(name="link", description="ativa/desativa o antilink.", guild=discord.Object(id=GUILD_ID))
async def link(interaction: discord.Interaction, estado: str):
    global antilink_ativo
    if not tem_cargo_soberba(interaction.user):
        await interaction.response.send_message("🚫 sem permissão.", ephemeral=True)
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

@bot.tree.command(name="falar", description="faz o bot enviar uma mensagem.", guild=discord.Object(id=GUILD_ID))
async def falar(interaction: discord.Interaction, mensagem: str):
    if not tem_cargo_soberba(interaction.user):
        await interaction.response.send_message("🚫 sem permissão.", ephemeral=True)
        return
    await interaction.response.send_message("✅ mensagem enviada.", ephemeral=True)
    await interaction.channel.send(mensagem)

@bot.tree.command(name="convidados", description="mostra quantos membros o usuário manteve no servidor.", guild=discord.Object(id=GUILD_ID))
async def convidados_cmd(interaction: discord.Interaction, usuario: discord.Member = None):
    usuario = usuario or interaction.user
    total = len(convidados.get(usuario.id, []))
    nomes = [interaction.guild.get_member(uid).display_name for uid in convidados.get(usuario.id, []) if interaction.guild.get_member(uid)]
    desc = f"{usuario.mention} manteve **{total} pessoas** no servidor.\n"
    if nomes:
        desc += "👥 " + ", ".join(nomes)
    embed = discord.Embed(title="👥 convites", description=desc, color=discord.Color.blurple())
    await interaction.response.send_message(embed=embed)

# -------------------------
# execução
# -------------------------
if __name__ == "__main__":
    token = os.getenv("TOKEN")
    if not token:
        print("❌ erro: variável TOKEN não encontrada.")
    else:
        bot.run(token)
