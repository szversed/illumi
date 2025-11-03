import discord
from discord import app_commands
from discord.ext import commands
import asyncio
import time
import os  # <- para pegar o token do Railway

intents = discord.Intents.default()
intents.guilds = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

# -------------------------
# Configurações de performance máxima
# -------------------------
CONCURRENCY = 20       # máximo de unbans concorrentes
MAX_RETRIES = 5        # tentativas em caso de erro
UPDATE_EVERY = 10      # atualiza embed a cada X desbanheados
UPDATE_SECONDS = 2     # ou a cada X segundos, o que ocorrer primeiro

@bot.event
async def on_ready():
    print(f"Logado como {bot.user}")
    try:
        await bot.tree.sync()
        print("Comandos sincronizados.")
    except Exception as e:
        print("Erro ao sincronizar comandos:", e)

# -------------------------
# Comando de desbanimento rápido
# -------------------------
@bot.tree.command(name="desbanirtudo", description="Desbaneia todos os bans do servidor na maior velocidade possível.")
@app_commands.describe(confirm="Confirme True para executar a operação (proteção contra uso acidental).")
async def desbanirtudo(interaction: discord.Interaction, confirm: bool):
    if interaction.guild is None:
        await interaction.response.send_message("❌ Este comando só pode ser usado dentro de um servidor.", ephemeral=True)
        return
    if not interaction.user.guild_permissions.ban_members:
        await interaction.response.send_message("❌ Você precisa da permissão **Ban Members** para usar este comando.", ephemeral=True)
        return
    if not confirm:
        await interaction.response.send_message("Ação cancelada — confirme passando `confirm: True`.", ephemeral=True)
        return

    await interaction.response.send_message("🔎 Buscando bans e iniciando desbanimento (máxima velocidade)...", ephemeral=False)
    msg = await interaction.original_response()

    try:
        bans = await interaction.guild.bans()
    except Exception as e:
        await msg.edit(content=f"❌ Erro ao buscar bans: {e}")
        return

    total = len(bans)
    if total == 0:
        embed = discord.Embed(title="Desbanir todos", description="Não há usuários banidos neste servidor.", color=0x2f3136)
        await msg.edit(content=None, embed=embed)
        return

    embed = discord.Embed(
        title="Desbanir todos — progresso",
        description=f"Iniciando desbanimento de **{total}** usuários...",
        color=0x2fce89
    )
    embed.add_field(name="Total", value=str(total), inline=True)
    embed.add_field(name="Desbanheados", value="0", inline=True)
    embed.add_field(name="Restantes", value=str(total), inline=True)
    embed.set_footer(text=f"Pedido por {interaction.user}", icon_url=interaction.user.display_avatar.url if interaction.user.display_avatar else None)
    await msg.edit(content=None, embed=embed)

    desbanheados = 0
    last_failed = []
    desbanheados_lock = asyncio.Lock()
    edit_lock = asyncio.Lock()
    last_edit_time = time.time()
    semaphore = asyncio.Semaphore(CONCURRENCY)

    async def try_unban(entry: discord.BanEntry):
        nonlocal desbanheados, last_edit_time
        user = entry.user
        attempts = 0
        while attempts < MAX_RETRIES:
            attempts += 1
            await semaphore.acquire()
            try:
                await interaction.guild.unban(user, reason=f"Desbanido em massa por {interaction.user}")
                async with desbanheados_lock:
                    desbanheados += 1

                now = time.time()
                should_update = False
                async with desbanheados_lock:
                    if (desbanheados % UPDATE_EVERY) == 0:
                        should_update = True
                if (now - last_edit_time) >= UPDATE_SECONDS:
                    should_update = True

                if should_update:
                    async with edit_lock:
                        try:
                            embed.set_field_at(1, name="Desbanheados", value=str(desbanheados), inline=True)
                            embed.set_field_at(2, name="Restantes", value=str(total - desbanheados), inline=True)
                            await msg.edit(embed=embed)
                            last_edit_time = time.time()
                        except Exception:
                            pass
                return True
            except discord.HTTPException:
                await asyncio.sleep(2 ** attempts * 0.1)
            except Exception as e:
                last_failed.append((user, str(e)))
                return False
            finally:
                try:
                    semaphore.release()
                except Exception:
                    pass

        last_failed.append((user, f"Falha após {MAX_RETRIES} tentativas"))
        return False

    tasks = [asyncio.create_task(try_unban(entry)) for entry in bans]
    await asyncio.gather(*tasks)

    try:
        async with edit_lock:
            embed.set_field_at(1, name="Desbanheados", value=str(desbanheados), inline=True)
            embed.set_field_at(2, name="Restantes", value=str(total - desbanheados), inline=True)
            await msg.edit(embed=embed)
    except Exception:
        pass

    final_embed = discord.Embed(
        title="Desbanir todos — concluído",
        description=f"✅ Operação finalizada. Desbanheados: {desbanheados}/{total}",
        color=0x2fce89
    )

    if last_failed:
        failed_list = "\n".join(f"{u} — {err}" for u, err in last_failed[:20])
        if len(last_failed) > 20:
            failed_list += f"\n... e mais {len(last_failed)-20} falhas."
        final_embed.add_field(name="Falhas", value=failed_list, inline=False)
    else:
        final_embed.add_field(name="Falhas", value="Nenhuma", inline=False)

    final_embed.set_footer(text=f"Pedido por {interaction.user}", icon_url=interaction.user.display_avatar.url if interaction.user.display_avatar else None)
    await msg.edit(embed=final_embed)

# -------------------------
# Run bot (Railway)
# -------------------------
TOKEN = os.getenv("TOKEN")
if not TOKEN: print("❌ ERRO: variável TOKEN não encontrada.")
else: bot.run(TOKEN)
