from __future__ import annotations

import random
import re
import math
from typing import Dict, Any, List, TYPE_CHECKING

from src import users
from src.decorators import command
from src.containers import UserDict
from src.functions import get_players, get_all_players, get_target, get_main_role, get_reveal_role
from src.messages import messages
from src.status import try_misdirection, try_exchange, add_dying, kill_players, add_absent
from src.events import Event, event_listener
from src.trans import chk_win
from src.cats import Wolf, Killer

if TYPE_CHECKING:
    from src.dispatcher import MessageDispatcher
    from src.gamestate import GameState
    from src.users import User

_rolestate = {} # type: Dict[str, Dict[str, Any]]

def setup_variables(rolename: str):
    GUNNERS: UserDict[User, int] = UserDict()
    _rolestate[rolename] = {
        "GUNNERS": GUNNERS
    }

    @command("shoot", playing=True, silenced=True, phases=("day",), roles=(rolename,))
    def shoot(wrapper: MessageDispatcher, message: str):
        """Use this to fire off a bullet at someone in the day if you have bullets."""
        if not GUNNERS[wrapper.source]:
            wrapper.pm(messages["no_bullets"])
            return

        var = wrapper.game_state

        target = get_target(wrapper, re.split(" +", message)[0], not_self_message="gunner_target_self")
        if not target:
            return

        target = try_misdirection(var, wrapper.source, target)
        if try_exchange(var, wrapper.source, target):
            return

        GUNNERS[wrapper.source] -= 1

        gun_evt = Event("gun_chances", {"hit": 0, "miss": 0, "headshot": 0})
        gun_evt.dispatch(var, wrapper.source, rolename)

        rand = random.random() # need to save it

        shoot_evt = Event("gun_shoot", {"hit": rand <= gun_evt.data["hit"], "kill": random.random() <= gun_evt.data["headshot"]})
        shoot_evt.dispatch(var, wrapper.source, target, rolename)

        realrole = get_main_role(var, target)
        targrole = get_reveal_role(var, target)

        if shoot_evt.data["hit"]:
            wrapper.send(messages["shoot_success"].format(wrapper.source, target))
            if realrole in Wolf and shoot_evt.data["kill"]:
                to_send = "gunner_victim_wolf_death_no_reveal"
                if var.role_reveal == "on":
                    to_send = "gunner_victim_wolf_death"
                wrapper.send(messages[to_send].format(target, targrole))
                add_dying(var, target, killer_role=get_main_role(var, wrapper.source), reason="gunner_victim")
                if kill_players(var):
                    return
            elif shoot_evt.data["kill"]:
                to_send = "gunner_victim_villager_death_accident"
                if gun_evt.data["headshot"] == 1: # would always headshot
                    to_send = "gunner_victim_villager_death"
                wrapper.send(messages[to_send].format(target))
                if var.role_reveal in ("on", "team"):
                    wrapper.send(messages["gunner_victim_role"].format(targrole))
                add_dying(var, target, killer_role=get_main_role(var, wrapper.source), reason="gunner_victim")
                if kill_players(var):
                    return
            else:
                wrapper.send(messages["gunner_victim_injured"].format(target))
                add_absent(var, target, "wounded")
                from src.votes import chk_decision
                if not chk_win(var):
                    # game didn't immediately end due to injury, see if we should force through a vote
                    chk_decision(var)

        elif rand <= gun_evt.data["hit"] + gun_evt.data["miss"]:
            wrapper.send(messages["gunner_miss"].format(wrapper.source))
        else: # BOOM! your gun explodes, you're dead
            to_send = "gunner_suicide_no_reveal"
            if var.role_reveal in ("on", "team"):
                to_send = "gunner_suicide"
            wrapper.send(messages[to_send].format(wrapper.source, get_reveal_role(var, wrapper.source)))
            add_dying(var, wrapper.source, killer_role="villager", reason="gunner_suicide") # blame explosion on villager's shoddy gun construction or something
            kill_players(var)

    @event_listener("send_role", listener_id="gunners.<{}>.on_send_role".format(rolename))
    def on_send_role(evt: Event, var: GameState):
        for gunner in get_all_players(var, (rolename,)):
            if GUNNERS[gunner] or var.always_pm_role:
                gunner.send(messages["{0}_notify".format(rolename)].format(GUNNERS[gunner]))

    @event_listener("transition_day_resolve_end", priority=4, listener_id="gunners.<{}>.on_transition_day_resolve_end".format(rolename))
    def on_transition_day_resolve_end(evt: Event, var: GameState, victims: List[User]):
        for victim in list(evt.data["dead"]):
            if GUNNERS.get(victim) and "@wolves" in evt.data["killers"][victim]:
                if random.random() < var.GUNNER_KILLS_WOLF_AT_NIGHT_CHANCE:
                    # pick a random wolf to be shot
                    wolves = [wolf for wolf in get_players(var, Wolf & Killer) if wolf not in evt.data["dead"]]
                    if wolves:
                        shot = random.choice(wolves)
                        event = Event("gun_shoot", {"hit": True, "kill": True})
                        event.dispatch(var, victim, shot, rolename)
                        GUNNERS[victim] -= 1  # deduct the used bullet
                        if event.data["hit"] and event.data["kill"]:
                            to_send = "gunner_killed_wolf_overnight_no_reveal"
                            if var.role_reveal in ("on", "team"):
                                to_send = "gunner_killed_wolf_overnight"
                            evt.data["message"][victim].append(messages[to_send].format(victim, shot, get_reveal_role(var, shot)))
                            evt.data["dead"].append(shot)
                            evt.data["killers"][shot].append(victim)
                        elif event.data["hit"]:
                            # shot hit, but didn't kill
                            evt.data["message"][victim].append(messages["gunner_shoot_overnight_hit"].format(victim))
                            add_absent(var, shot, "wounded")
                        else:
                            # shot was fired and missed
                            evt.data["message"][victim].append(messages["gunner_shoot_overnight_missed"].format(victim))

                # let wolf steal gun if the gunner has any bullets remaining
                # this gives the looter the "wolf gunner" secondary role
                # if the wolf gunner role isn't loaded, guns cannot be stolen regardless of var.WOLF_STEALS_GUN
                if var.WOLF_STEALS_GUN and GUNNERS[victim] and "wolf gunner" in _rolestate:
                    possible = get_players(var, Wolf & Killer)
                    random.shuffle(possible)
                    for looter in possible:
                        if looter not in evt.data["dead"]:
                            break
                    else:
                        return # no live wolf, nothing to do here

                    _rolestate["wolf gunner"]["GUNNERS"][looter] = _rolestate["wolf gunner"]["GUNNERS"].get(looter, 0) + 1
                    del GUNNERS[victim]
                    var.roles["wolf gunner"].add(looter)
                    looter.send(messages["wolf_gunner"].format(victim))

    @event_listener("myrole", listener_id="gunners.<{}>.on_myrole".format(rolename))
    def on_myrole(evt: Event, var: GameState, user: User):
        if GUNNERS.get(user):
            evt.data["messages"].append(messages["gunner_myrole"].format(rolename, GUNNERS[user]))

    @event_listener("revealroles_role", listener_id="gunners.<{}>.on_revealroles_role".format(rolename))
    def on_revealroles_role(evt: Event, var: GameState, user: User, role: str):
        if role == rolename and user in GUNNERS:
            evt.data["special_case"].append(messages["gunner_revealroles"].format(GUNNERS[user]))

    @event_listener("reset", listener_id="gunners.<{}>.on_reset".format(rolename))
    def on_reset(evt: Event, var: GameState):
        GUNNERS.clear()

    @event_listener("new_role", listener_id="gunners.<{}>.on_new_role".format(rolename))
    def on_new_role(evt: Event, var: GameState, user: User, old_role: str):
        if old_role == rolename:
            if evt.data["role"] != rolename:
                del GUNNERS[user]

        elif evt.data["role"] == rolename:
            bullets = math.ceil(var.SHOTS_MULTIPLIER[rolename] * len(get_players(var)))
            event = Event("gun_bullets", {"bullets": bullets})
            event.dispatch(var, user, rolename)
            GUNNERS[user] = event.data["bullets"]

    return GUNNERS

@event_listener("gun_chances")
def on_gun_chances(evt: Event, var: GameState, user: User, role: str):
    if role in var.GUN_CHANCES:
        hit, miss, headshot = var.GUN_CHANCES[role]
        evt.data["hit"] += hit
        evt.data["miss"] += miss
        evt.data["headshot"] += headshot
