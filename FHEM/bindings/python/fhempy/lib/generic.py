import asyncio
import functools
import inspect
import json
import os

from fhempy.lib import fhem

from . import utils


class FhemModule:
    def __init__(self, logger):
        self.logger = logger
        self.loop = asyncio.get_event_loop()
        self._tasks = []
        self._conf_set = {}
        self._conf_get = {}
        self._conf_attr = {}

    async def set_attr_config(self, attr_config):
        self._conf_attr = attr_config
        await utils.handle_define_attr(self._conf_attr, self, self.hash)

    async def set_set_config(self, set_config):
        self._conf_set = set_config
        await fhem.send_default_response(
            self.hash,
            await utils.handle_set(self._conf_set, self, self.hash, ["?"], {}),
        )

    async def set_get_config(self, get_config):
        """Configure get commands and their FHEMWEB help.

        Uses the same options and parameters as set_set_config. Commands call
        get_<command>(hash, params), unless a custom "function" is configured.
        An existing module-specific Get method continues to take precedence.
        """
        self._conf_get = get_config

    async def set_icon(self, icon_name):
        curr_icon = await fhem.AttrVal(self.hash["NAME"], "icon", "")
        if curr_icon == "":
            await fhem.CommandAttr(self.hash, f"{self.hash['NAME']} icon {icon_name}")

    # FHEM FUNCTION
    async def FW_detailFn(self, hash, args, argsh):
        """Read current module help for each detail page, preserving Markdown."""
        (FW_wname, d, room, pageHash) = args

        if not hasattr(self, "hash"):
            return (
                "This device is disabled, set disable attribute "
                "to 0 or delete it to enable the device<br>"
            )

        ret = """<script type="text/javascript">
        function displayHelp() {
          var x = document.getElementById("fhempyReadme");
          if (x.style.display === "none") {
            x.style.display = "block";
          } else {
            x.style.display = "none";
          }
          var off = $("#fhempyReadme").position().top-20;
          $('body, html').animate({scrollTop:off}, 500);
        }
        
        $(document).ready(function() {
          $("#content").append('<div class="makeTable help" id="fhempyReadme"></div>');
          $("#fhempyReadme").html(###README_HELP_STRING###);
          document.getElementById("fhempyReadme").style.display = "none";

          var helpConfig = ###COMMAND_HELP_CONFIG###;
          function bindHelp(kind, helpId, before) {
            function updateHelp() {
              var cmd = $("select." + kind).val();
              var help = helpConfig[kind][cmd];
              if (!help) {
                $("#" + helpId).remove();
                return;
              }
              if (document.getElementById(helpId) === null) {
                $("<div>", {id: helpId, "class": "makeTable help"})
                  .insertBefore(before);
              }
              $("#" + helpId).html(help + "<br>");
            }
            $("select." + kind).change(updateHelp);
            updateHelp();
          }
          bindHelp("set", "idCmdHelp", "div.makeTable.internals");
          bindHelp("get", "idGetHelp", "div.makeTable.internals");
          bindHelp("attr", "idAttrHelp", "div.makeTable.attributes");

          var helpLink = document.getElementById("content")
            .getElementsByClassName("detLink");
          if (helpLink.length == 0) {
            helpLink = document.getElementById("content")
              .getElementsByClassName("detLink devSpecHelp");
            helpLink[0].innerHTML = '<div class="detLink devSpecHelp"><a href="#" onclick="displayHelp();return false;">Device specific help</a></div>';
          } else {
            helpLink[0].innerHTML = '<div class="detLink"><a href="#" onclick="displayHelp();return false;">Help for fhempy device ###DEVICETYPE###</a></div>';
          }
        });
        </script>"""
        help_config = {
            kind: {
                command: config["help"].replace("\n", "<br>")
                for command, config in commands.items()
                if "help" in config
            }
            for kind, commands in (
                ("set", self._conf_set),
                ("get", self._conf_get),
                ("attr", self._conf_attr),
            )
        }
        ret = ret.replace(
            "###COMMAND_HELP_CONFIG###",
            json.dumps(help_config).replace("<", "\\u003c"),
        )

        ret = ret.replace(
            "###DEVICETYPE###",
            self.hash["FHEMPYTYPE"],
        )

        # Reload on each detail page request so README edits need no restart.
        readme_str = await utils.run_blocking(
            functools.partial(self._get_readme_content)
        )
        ret = ret.replace(
            "###README_HELP_STRING###",
            json.dumps(readme_str).replace("<", "\\u003c"),
        )

        return ret

    def _get_readme_content(self):
        """Read and convert the module's Markdown, including fenced code blocks."""
        import markdown2
        from fhempy import lib

        initfile = inspect.getfile(lib)
        fhempy_root = os.path.dirname(initfile)
        try:
            readme_md = ""
            with open(
                fhempy_root + "/" + self.hash["FHEMPYTYPE"] + "/README.md", "r"
            ) as f:
                readme_md = f.read()

            readme_str = markdown2.markdown(readme_md, extras=["fenced-code-blocks"])
            return readme_str
            # make_html_images_inline(
            #    readme_str, fhempy_root + "/" + self.hash["FHEMPYTYPE"] + "/"
            # )

        except FileNotFoundError:
            return f"No README.md file found for {self.hash['FHEMPYTYPE']}."

    @staticmethod
    def validate_define(args, argsh):
        """Optionally return an error/usage string, or None for valid arguments.

        This must be a quick, synchronous argument check without I/O or state
        changes. The binding calls it before replacing an initialized device
        of the same type, and after loading a module for a new device. First
        loads are already acknowledged; their errors are reported in state.

        Keep validation rules and usage text in the module. Define may reuse
        this hook, including when running with an older binding. Modules that
        do not override it retain their existing behaviour.
        """
        return None

    # FHEM FUNCTION
    async def Define(self, hash, args, argsh):
        self.hash = hash
        self._defargs = args
        self._defargsh = argsh

        # send set to fhem if set_set_config wasn't used before
        if self._conf_set == {}:
            await self.set_set_config(self._conf_set)

        info = await utils.handle_define_attr(
            self._conf_attr, self, self.hash, {"room": "", "group": ""}
        )
        if info["init_done"] == 1:
            if info["attr"]["room"] == "":
                await fhem.CommandAttr(self.hash, f"{self.hash['NAME']} room fhempy")
            if info["attr"]["group"] == "":
                await fhem.CommandAttr(
                    self.hash,
                    (f"{self.hash['NAME']} group " f"{self.hash['FHEMPYTYPE']}"),
                )

    # FHEM FUNCTION
    async def Attr(self, hash, args, argsh):
        return await utils.handle_attr(self._conf_attr, self, hash, args, argsh)

    # FHEM FUNCTION
    async def Set(self, hash, args, argsh):
        return await utils.handle_set(self._conf_set, self, hash, args, argsh)

    # FHEM FUNCTION
    async def Get(self, hash, args, argsh):
        # Preserve the previous empty reply for modules without get commands.
        if not self._conf_get:
            return ""
        return await utils.handle_get(self._conf_get, self, hash, args, argsh)

    def create_async_task(self, coro):
        task = asyncio.create_task(self._run_coro(coro))
        task.add_done_callback(self._handle_task_result)
        self._tasks.append(task)
        return task

    async def _run_coro(self, coro):
        try:
            await coro
        except asyncio.CancelledError:
            pass

    def _handle_task_result(self, task):
        if task in self._tasks:
            self._tasks.remove(task)
        try:
            task.result()
        except asyncio.CancelledError:
            pass
        except Exception:
            self.logger.exception("Exception raised by task: %r", task)

    def cancel_async_task(self, task):
        if task in self._tasks:
            self._tasks.remove(task)
        task.cancel()

    # FHEM FUNCTION
    async def Undefine(self, hash):
        # cancel all tasks, Define might be called again (attr disable 0)
        tasks = self._tasks
        self._tasks = []
        for task in tasks:
            task.cancel()
