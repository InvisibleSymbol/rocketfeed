import io
import logging
from datetime import datetime, timezone

from discord import app_commands, Interaction, Message, ui, TextStyle, AllowedMentions, ButtonStyle, File, TextChannel, \
    ChannelType, User
from discord.app_commands import Group, Choice
from discord.ext.commands import Cog, GroupCog
from motor.motor_asyncio import AsyncIOMotorClient

from utils.cfg import cfg
from utils.embeds import Embed
from utils.get_or_fetch import get_or_fetch_channel

log = logging.getLogger("support-threads")
log.setLevel(cfg["log_level"])


async def generate_template_embed(db, template_name: str):
    # get the boiler message from the database
    template = await db.support_bot.find_one({'_id': template_name})
    # generate the embed
    return Embed(title=template['title'], description=template['description'])


# Define a simple View that gives us a counter button
class AdminView(ui.View):
    def __init__(self, db: AsyncIOMotorClient, template_name: str):
        super().__init__()
        self.db = db
        self.template_name = template_name

    @ui.button(label='Edit', style=ButtonStyle.blurple)
    async def edit(self, interaction: Interaction, button: ui.Button):
        boiler = await self.db.support_bot.find_one({'_id': self.template_name})
        # Make sure to update the message with our update
        await interaction.response.send_modal(AdminModal(boiler["title"], boiler["description"], self.db, self.template_name))


class AdminModal(ui.Modal,
                 title="Change Template Message"):
    def __init__(self, old_title, old_description, db, template_name):
        super().__init__()
        self.db = db
        self.old_title = old_title
        self.old_description = old_description
        self.template_name = template_name
        self.title_field = ui.TextInput(
            label="Title",
            placeholder="Enter a title",
            default=old_title)
        self.description_field = ui.TextInput(
            label="Description",
            placeholder="Enter a description",
            default=old_description,
            style=TextStyle.paragraph,
            max_length=4000)
        self.add_item(self.title_field)
        self.add_item(self.description_field)

    async def on_submit(self, interaction: Interaction) -> None:
        # get the data from the db
        template = await self.db.support_bot.find_one({'_id': self.template_name})
        # verify that no changes were made while we were editing
        if template["title"] != self.old_title or template["description"] != self.old_description:
            # dump the description into a memory file
            with io.StringIO(self.description_field.value) as f:
                await interaction.response.edit_message(
                    embed=Embed(
                        description="Someone made changes while you were editing. Please try again.\n"
                                    "Your pending changes have been attached to this message."), view=None)
                a = await interaction.original_response()
                await a.add_files(File(fp=f, filename="pending_description_dump.txt"))
            return
        try:
            await self.db.support_bot_dumps.insert_one(
                {
                    "ts"      : datetime.now(timezone.utc),
                    "template": self.template_name,
                    "prev"    : template,
                    "new"     : {
                        "title"      : self.title_field.value,
                        "description": self.description_field.value
                    },
                    "author"  : {
                        "id"  : interaction.user.id,
                        "name": interaction.user.name
                    }
                })
        except Exception as e:
            log.error(e)

        await self.db.support_bot.update_one(
            {"_id": self.template_name},
            {"$set": {"title": self.title_field.value, "description": self.description_field.value}})
        embeds = [Embed(), await generate_template_embed(self.db, self.template_name)]
        embeds[0].title = f"View & Edit {self.template_name} template"
        embeds[0].description = f"The following is a preview of the {self.template_name} template.\n" \
                                f"You can edit this template by clicking the 'Edit' button."
        await interaction.response.edit_message(embeds=embeds, view=AdminView(self.db, self.template_name))


def has_perms(interaction: Interaction):
    return any([
        cfg["rocketpool.support.role_id"] in [r.id for r in interaction.user.roles],
        cfg["discord.owner.user_id"] == interaction.user.id
    ])


class SupportUtils(GroupCog, name="support"):
    subgroup = Group(name='template', description='various templates used by active support members',
                     guild_ids=[cfg["rocketpool.support.server_id"]])

    def __init__(self, bot):
        self.bot = bot
        self.db = AsyncIOMotorClient(cfg["mongodb_uri"]).get_database("rocketwatch")
        self.ctx_menu = app_commands.ContextMenu(
            name='New Support Thread',
            callback=self.my_cool_context_menu,
            guild_ids=[cfg["rocketpool.support.server_id"]]
        )
        self.bot.tree.add_command(self.ctx_menu)

    @Cog.listener()
    async def on_ready(self):
        # insert the boiler message into the database, if it doesn't already exist
        await self.db.support_bot.update_one(
            {'_id': 'boiler'},
            {'$setOnInsert': {
                'title'      : 'Support Message',
                'description': 'This is an support message.'
            }},
            upsert=True
        )

    async def cog_unload(self) -> None:
        self.bot.tree.remove_command(self.ctx_menu.name, type=self.ctx_menu.type)

    # You can add checks too
    @app_commands.guilds(cfg["rocketpool.support.server_id"])
    async def my_cool_context_menu(self, interaction: Interaction, message: Message):
        if not has_perms(interaction):
            await interaction.response.send_message(
                embed=Embed(title="Error", description="You do not have permission to use this command."), ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        author = message.author
        initiator = interaction.user
        try:
            target = message
            args = {}
            if message.channel.id != cfg["rocketpool.support.channel_id"]:
                # create a new thread in the support channel
                target = await get_or_fetch_channel(self.bot, cfg["rocketpool.support.channel_id"])
                args = {"type": ChannelType.public_thread}

            a = await target.create_thread(name=f"{author} - Support Thread",
                                           reason=f"Support Thread ({author}): triggered by {initiator}",
                                           auto_archive_duration=60,
                                           **args)
            suffix = ""
            if isinstance(target, TextChannel):
                suffix = f"\nOriginal Message: {message.jump_url}"
            await a.send(
                content=f"Original Message Author: {author.mention}\nSupport Thread Initiator: {initiator.mention}{suffix}",
                embed=await generate_template_embed(self.db, "boiler"),
                allowed_mentions=AllowedMentions(users=True))
            # send reply to original message with a link to the new thread
            await message.reply(f"{author.mention}, an support thread has been created for you,"
                                f" please move to {a.mention} for further assistance.", mention_author=True)
            await interaction.edit_original_response(
                embed=Embed(
                    title="Support Thread Successfully Created",
                    description=f"[Thread Link]({a.jump_url})")
            )
        except Exception as e:
            await interaction.edit_original_response(
                embed=Embed(
                    title="Error",
                    description=f"{e}"
                ),
            )
            raise e

    @subgroup.command()
    async def add(self, interaction: Interaction, name: str):
        if not has_perms(interaction):
            await interaction.response.send_message(
                embed=Embed(title="Error", description="You do not have permission to use this command."), ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        # check if the template already exists in the db
        if await self.db.support_bot.find_one({"_id": name}):
            await interaction.edit_original_response(
                embed=Embed(
                    title="Error",
                    description=f"A template with the name '{name}' already exists."
                ),
            )
            return
        # create the template in the db
        await self.db.support_bot.insert_one(
            {"_id": name, "title": "Insert Title here", "description": "Insert Description here"})
        embeds = [Embed(), await generate_template_embed(self.db, name)]
        embeds[0].title = f"View & Edit {name} template"
        embeds[0].description = f"The following is a preview of the {name} template.\n" \
                                f"You can edit this template by clicking the 'Edit' button."
        await interaction.edit_original_response(embeds=embeds, view=AdminView(self.db, name))

    @subgroup.command()
    async def edit(self, interaction: Interaction, name: str):
        if not has_perms(interaction):
            await interaction.response.send_message(
                embed=Embed(title="Error", description="You do not have permission to use this command."), ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        # check if the template exists in the db
        template = await self.db.support_bot.find_one({"_id": name})

        if not template:
            await interaction.edit_original_response(
                embed=Embed(
                    title="Error",
                    description=f"A template with the name '{name}' does not exist."
                ),
            )
            return
        embeds = [Embed(), await generate_template_embed(self.db, name)]
        embeds[0].title = f"View & Edit {name} template"
        embeds[0].description = f"The following is a preview of the {name} template.\n" \
                                f"You can edit this template by clicking the 'Edit' button."
        # respond with the edit view
        await interaction.edit_original_response(embeds=embeds, view=AdminView(self.db, name))

    @subgroup.command()
    async def remove(self, interaction: Interaction, name: str):
        if not has_perms(interaction):
            await interaction.response.send_message(
                embed=Embed(title="Error", description="You do not have permission to use this command."), ephemeral=True)
            return
        if name == "boiler":
            await interaction.edit_original_response(
                embed=Embed(
                    title="Error",
                    description=f"The template '{name}' cannot be removed."
                ),
            )
            return
        await interaction.response.defer(ephemeral=True)
        # check if the template exists in the db
        template = await self.db.support_bot.find_one({"_id": name})
        if not template:
            await interaction.edit_original_response(
                embed=Embed(
                    title="Error",
                    description=f"A template with the name '{name}' does not exist."
                ),
            )
            return
        # remove the template from the db
        await self.db.support_bot.delete_one({"_id": name})
        await interaction.edit_original_response(
            embed=Embed(
                title="Success",
                description=f"Template '{name}' removed."
            ),
        )

    @subgroup.command()
    async def use(self, interaction: Interaction, name: str, mention: User | None):
        # check if the template exists in the db
        template = await self.db.support_bot.find_one({"_id": name})
        if not template:
            await interaction.response.send_message(
                embed=Embed(
                    title="Error",
                    description=f"A template with the name '{name}' does not exist."
                ),
                ephemeral=True
            )
            return
        if name == "boiler":
            await interaction.response.send_message(
                embed=Embed(
                    title="Error",
                    description=f"The template '{name}' cannot be used."
                ),
                ephemeral=True
            )
            return
        # respond with the template embed
        await interaction.response.send_message(
            content=mention.mention if mention else "",
            embed=Embed(
                title=template["title"],
                description=template["description"]
            ))

    @edit.autocomplete("name")
    @remove.autocomplete("name")
    @use.autocomplete("name")
    async def match_template(self, interaction: Interaction, current: str):
        return [
            Choice(
                name=c["_id"],
                value=c["_id"]
            ) for c in await self.db.support_bot.find(
                {
                    "_id": {
                        "$regex": current,
                        "$ne"   : "boiler" if interaction.command.name != "edit" else None
                    }
                }
            ).to_list(None)
        ]


async def setup(self):
    await self.add_cog(SupportUtils(self))
