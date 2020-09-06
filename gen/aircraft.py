from dataclasses import dataclass

from dcs import helicopters
from dcs.action import ActivateGroup, AITaskPush, MessageToAll
from dcs.condition import TimeAfter, CoalitionHasAirdrome, PartOfCoalitionInZone
from dcs.flyingunit import FlyingUnit
from dcs.helicopters import helicopter_map, UH_1H
from dcs.terrain.terrain import Airport, NoParkingSlotError
from dcs.triggers import TriggerOnce, Event

from game.data.cap_capabilities_db import GUNFIGHTERS
from game.settings import Settings
from game.utils import nm_to_meter
from gen.airfields import RunwayData
from gen.flights.ai_flight_planner import FlightPlanner
from gen.flights.flight import (
    Flight,
    FlightType,
    FlightWaypoint,
    FlightWaypointType,
)
from gen.radios import get_radio, MHz, Radio, RadioFrequency, RadioRegistry
from .conflictgen import *
from .naming import *

WARM_START_HELI_AIRSPEED = 120
WARM_START_HELI_ALT = 500
WARM_START_ALTITUDE = 3000
WARM_START_AIRSPEED = 550

CAP_DURATION = 30 # minutes

RTB_ALTITUDE = 800
RTB_DISTANCE = 5000
HELI_ALT = 500

# Note that fallback radio channels will *not* be reserved. It's possible that
# flights using these will overlap with other channels. This is because we would
# need to make sure we fell back to a frequency that is not used by any beacon
# or ATC, which we don't have the information to predict. Deal with the minor
# annoyance for now since we'll be fleshing out radio info soon enough.
ALLIES_WW2_CHANNEL = MHz(124)
GERMAN_WW2_CHANNEL = MHz(40)
HELICOPTER_CHANNEL = MHz(127)
UHF_FALLBACK_CHANNEL = MHz(251)


@dataclass(frozen=True)
class AircraftData:
    """Additional aircraft data not exposed by pydcs."""

    #: The type of radio used for intra-flight communications.
    intra_flight_radio: Radio

    #: Index of the radio used for intra-flight communications. Matches the
    #: index of the panel_radio field of the pydcs.dcs.planes object.
    inter_flight_radio_index: Optional[int]

    #: Index of the radio used for intra-flight communications. Matches the
    #: index of the panel_radio field of the pydcs.dcs.planes object.
    intra_flight_radio_index: Optional[int]


# Indexed by the id field of the pydcs PlaneType.
AIRCRAFT_DATA: Dict[str, AircraftData] = {
    "A-10C": AircraftData(
        get_radio("AN/ARC-186(V) AM"),
        # The A-10's radio works differently than most aircraft. Doesn't seem to
        # be a way to set these from the mission editor, let alone pydcs.
        inter_flight_radio_index=None,
        intra_flight_radio_index=None
    ),
    "F-16C_50": AircraftData(
        get_radio("AN/ARC-222"),
        # COM2 is the AN/ARC-222, which is the VHF radio we want to use for
        # intra-flight communication to leave COM1 open for UHF inter-flight.
        inter_flight_radio_index=1,
        intra_flight_radio_index=2
    ),
    "FA-18C_hornet": AircraftData(
        get_radio("AN/ARC-210"),
        # DCS will clobber channel 1 of the first radio compatible with the
        # flight's assigned frequency. Since the F/A-18's two radios are both
        # AN/ARC-210s, radio 1 will be compatible regardless of which frequency
        # is assigned, so we must use radio 1 for the intra-flight radio.
        inter_flight_radio_index=2,
        intra_flight_radio_index=1
    ),
}


# TODO: Get radio information for all the special cases.
def get_fallback_channel(unit_type: UnitType) -> RadioFrequency:
    if unit_type in helicopter_map.values() and unit_type != UH_1H:
        return HELICOPTER_CHANNEL

    german_ww2_aircraft = [
        Bf_109K_4,
        FW_190A8,
        FW_190D9,
        Ju_88A4,
    ]

    if unit_type in german_ww2_aircraft:
        return GERMAN_WW2_CHANNEL

    allied_ww2_aircraft = [
        I_16,
        P_47D_30,
        P_51D,
        P_51D_30_NA,
        SpitfireLFMkIX,
        SpitfireLFMkIXCW,
    ]

    if unit_type in allied_ww2_aircraft:
        return ALLIES_WW2_CHANNEL

    return UHF_FALLBACK_CHANNEL


@dataclass(frozen=True)
class ChannelAssignment:
    radio_id: int
    channel: int

    @property
    def radio_name(self) -> str:
        """Returns the name of the radio, i.e. COM1."""
        return f"COM{self.radio_id}"


@dataclass
class FlightData:
    """Details of a planned flight."""

    flight_type: FlightType

    #: All units in the flight.
    units: List[FlyingUnit]

    #: Total number of aircraft in the flight.
    size: int

    #: True if this flight belongs to the player's coalition.
    friendly: bool

    #: Number of minutes after mission start the flight is set to depart.
    departure_delay: int

    #: Arrival airport.
    arrival: RunwayData

    #: Departure airport.
    departure: RunwayData

    #: Diver airport.
    divert: Optional[RunwayData]

    #: Waypoints of the flight plan.
    waypoints: List[FlightWaypoint]

    #: Radio frequency for intra-flight communications.
    intra_flight_channel: RadioFrequency

    #: Map of radio frequencies to their assigned radio and channel, if any.
    frequency_to_channel_map: Dict[RadioFrequency, ChannelAssignment]

    def __init__(self, flight_type: FlightType, units: List[FlyingUnit],
                 size: int, friendly: bool, departure_delay: int,
                 departure: RunwayData, arrival: RunwayData,
                 divert: Optional[RunwayData], waypoints: List[FlightWaypoint],
                 intra_flight_channel: RadioFrequency) -> None:
        self.flight_type = flight_type
        self.units = units
        self.size = size
        self.friendly = friendly
        self.departure_delay = departure_delay
        self.departure = departure
        self.arrival = arrival
        self.divert = divert
        self.waypoints = waypoints
        self.intra_flight_channel = intra_flight_channel
        self.frequency_to_channel_map = {}

        self.assign_intra_flight_channel()

    @property
    def client_units(self) -> List[FlyingUnit]:
        """List of playable units in the flight."""
        return [u for u in self.units if u.is_human()]

    def assign_intra_flight_channel(self) -> None:
        """Assigns a channel to the intra-flight frequency."""
        if not self.client_units:
            return

        # pydcs will actually set up the channel for us, but we want to make
        # sure that it ends up in frequency_to_channel_map.
        try:
            data = AIRCRAFT_DATA[self.aircraft_type.id]
            self.assign_channel(
                data.intra_flight_radio_index, 1, self.intra_flight_channel)
        except KeyError:
            logging.warning(f"No aircraft data for {self.aircraft_type.id}")

    @property
    def aircraft_type(self) -> FlyingType:
        """Returns the type of aircraft in this flight."""
        return self.units[0].unit_type

    def num_radio_channels(self, radio_id: int) -> int:
        """Returns the number of preset channels for the given radio."""
        # Note: pydcs only initializes the radio presets for client slots.
        return self.client_units[0].num_radio_channels(radio_id)

    def channel_for(
            self, frequency: RadioFrequency) -> Optional[ChannelAssignment]:
        """Returns the radio and channel number for the given frequency."""
        return self.frequency_to_channel_map.get(frequency, None)

    def assign_channel(self, radio_id: int, channel_id: int,
                       frequency: RadioFrequency) -> None:
        """Assigns a preset radio channel to the given frequency."""
        for unit in self.client_units:
            unit.set_radio_channel_preset(radio_id, channel_id, frequency.mhz)

        # One frequency could be bound to multiple channels. Prefer the first,
        # since with the current implementation it will be the lowest numbered
        # channel.
        if frequency not in self.frequency_to_channel_map:
            self.frequency_to_channel_map[frequency] = ChannelAssignment(
                radio_id, channel_id
            )


class AircraftConflictGenerator:
    escort_targets = [] # type: typing.List[typing.Tuple[FlyingGroup, int]]

    def __init__(self, mission: Mission, conflict: Conflict, settings: Settings,
                 game, radio_registry: RadioRegistry):
        self.m = mission
        self.game = game
        self.settings = settings
        self.conflict = conflict
        self.radio_registry = radio_registry
        self.escort_targets = []
        self.flights: List[FlightData] = []

    def get_intra_flight_channel(
            self, airframe: UnitType) -> Tuple[int, RadioFrequency]:
        """Allocates an intra-flight channel to a group.

        Args:
            airframe: The type of aircraft a channel should be allocated for.

        Returns:
            A tuple of the radio index (for aircraft with multiple radios) and
            the frequency of the intra-flight channel.
        """
        try:
            aircraft_data = AIRCRAFT_DATA[airframe.id]
            channel = self.radio_registry.alloc_for_radio(
                aircraft_data.intra_flight_radio)
            return aircraft_data.intra_flight_radio_index, channel
        except KeyError:
            return 1, get_fallback_channel(airframe)

    def _start_type(self) -> StartType:
        return self.settings.cold_start and StartType.Cold or StartType.Warm

    def _setup_group(self, group: FlyingGroup, for_task: typing.Type[Task],
                     flight: Flight, dynamic_runways: Dict[str, RunwayData]):
        did_load_loadout = False
        unit_type = group.units[0].unit_type

        if unit_type in db.PLANE_PAYLOAD_OVERRIDES:
            override_loadout = db.PLANE_PAYLOAD_OVERRIDES[unit_type]
            if type(override_loadout) == dict:

                # Clear pylons
                for p in group.units:
                    p.pylons.clear()

                # Now load loadout
                if for_task in db.PLANE_PAYLOAD_OVERRIDES[unit_type]:
                    payload_name = db.PLANE_PAYLOAD_OVERRIDES[unit_type][for_task]
                    group.load_loadout(payload_name)
                    did_load_loadout = True
                    logging.info("Loaded overridden payload for {} - {} for task {}".format(unit_type, payload_name, for_task))
                elif "*" in db.PLANE_PAYLOAD_OVERRIDES[unit_type]:
                    payload_name = db.PLANE_PAYLOAD_OVERRIDES[unit_type]["*"]
                    group.load_loadout(payload_name)
                    did_load_loadout = True
                    logging.info("Loaded overridden payload for {} - {} for task {}".format(unit_type, payload_name, for_task))
            elif issubclass(override_loadout, MainTask):
                group.load_task_default_loadout(override_loadout)
                did_load_loadout = True

        if not did_load_loadout:
            group.load_task_default_loadout(for_task)

        if unit_type in db.PLANE_LIVERY_OVERRIDES:
            for unit_instance in group.units:
                unit_instance.livery_id = db.PLANE_LIVERY_OVERRIDES[unit_type]

        single_client = flight.client_count == 1
        for idx in range(0, min(len(group.units), flight.client_count)):
            unit = group.units[idx]
            if single_client:
                unit.set_player()
            else:
                unit.set_client()

            # Do not generate player group with late activation.
            if group.late_activation:
                group.late_activation = False

            # Set up F-14 Client to have pre-stored alignement
            if unit_type is F_14B:
                unit.set_property(F_14B.Properties.INSAlignmentStored.id, True)


        group.points[0].tasks.append(OptReactOnThreat(OptReactOnThreat.Values.EvadeFire))

        radio_id, channel = self.get_intra_flight_channel(unit_type)
        group.set_frequency(channel.mhz, radio_id)

        # TODO: Support for different departure/arrival airfields.
        cp = flight.from_cp
        fallback_runway = RunwayData(cp.full_name, runway_name="")
        if cp.cptype == ControlPointType.AIRBASE:
            # TODO: Implement logic for picking preferred runway.
            runway = flight.from_cp.airport.runways[0]
            runway_number = runway.heading // 10
            runway_side = ["", "L", "R"][runway.leftright]
            runway_name = f"{runway_number:02}{runway_side}"
            departure_runway = RunwayData.for_airfield(
                flight.from_cp.airport, runway_name)
        elif cp.is_fleet:
            departure_runway = dynamic_runways.get(cp.name, fallback_runway)
        else:
            logging.warning(f"Unhandled departure control point: {cp.cptype}")
            departure_runway = fallback_runway

        self.flights.append(FlightData(
            flight_type=flight.flight_type,
            units=group.units,
            size=len(group.units),
            friendly=flight.from_cp.captured,
            departure_delay=flight.scheduled_in,
            departure=departure_runway,
            arrival=departure_runway,
            # TODO: Support for divert airfields.
            divert=None,
            waypoints=flight.points,
            intra_flight_channel=channel
        ))

        # Special case so Su 33 carrier take off
        if unit_type is Su_33:
            if task is not CAP:
                for unit in group.units:
                    unit.fuel = Su_33.fuel_max / 2.2
            else:
                for unit in group.units:
                    unit.fuel = Su_33.fuel_max * 0.8


    def _generate_at_airport(self, name: str, side: Country, unit_type: FlyingType, count: int, client_count: int, airport: Airport = None, start_type = None) -> FlyingGroup:
        assert count > 0
        assert unit is not None

        if start_type is None:
            start_type = self._start_type()

        logging.info("airgen: {} for {} at {}".format(unit_type, side.id, airport))
        return self.m.flight_group_from_airport(
            country=side,
            name=name,
            aircraft_type=unit_type,
            airport=airport,
            maintask=None,
            start_type=start_type,
            group_size=count,
            parking_slots=None)

    def _generate_inflight(self, name: str, side: Country, unit_type: FlyingType, count: int, client_count: int, at: Point) -> FlyingGroup:
        assert count > 0
        assert unit is not None

        if unit_type in helicopters.helicopter_map.values():
            alt = WARM_START_HELI_ALT
            speed = WARM_START_HELI_AIRSPEED
        else:
            alt = WARM_START_ALTITUDE
            speed = WARM_START_AIRSPEED

        pos = Point(at.x + random.randint(100, 1000), at.y + random.randint(100, 1000))

        logging.info("airgen: {} for {} at {} at {}".format(unit_type, side.id, alt, speed))
        group = self.m.flight_group(
            country=side,
            name=name,
            aircraft_type=unit_type,
            airport=None,
            position=pos,
            altitude=alt,
            speed=speed,
            maintask=None,
            start_type=self._start_type(),
            group_size=count)

        group.points[0].alt_type = "RADIO"
        return group

    def _generate_at_group(self, name: str, side: Country, unit_type: FlyingType, count: int, client_count: int, at: typing.Union[ShipGroup, StaticGroup], start_type=None) -> FlyingGroup:
        assert count > 0
        assert unit is not None

        if start_type is None:
            start_type = self._start_type()

        logging.info("airgen: {} for {} at unit {}".format(unit_type, side.id, at))
        return self.m.flight_group_from_unit(
            country=side,
            name=name,
            aircraft_type=unit_type,
            pad_group=at,
            maintask=None,
            start_type=start_type,
            group_size=count)

    def _generate_group(self, name: str, side: Country, unit_type: FlyingType, count: int, client_count: int, at: db.StartingPosition):
        if isinstance(at, Point):
            return self._generate_inflight(name, side, unit_type, count, client_count, at)
        elif isinstance(at, Group):
            takeoff_ban = unit_type in db.CARRIER_TAKEOFF_BAN
            ai_ban = client_count == 0 and self.settings.only_player_takeoff

            if not takeoff_ban and not ai_ban:
                return self._generate_at_group(name, side, unit_type, count, client_count, at)
            else:
                return self._generate_inflight(name, side, unit_type, count, client_count, at.position)
        elif issubclass(at, Airport):
            takeoff_ban = unit_type in db.TAKEOFF_BAN
            ai_ban = client_count == 0 and self.settings.only_player_takeoff

            if not takeoff_ban and not ai_ban:
                try:
                    return self._generate_at_airport(name, side, unit_type, count, client_count, at)
                except NoParkingSlotError:
                    logging.info("No parking slot found at " + at.name + ", switching to air start.")
                    pass
            return self._generate_inflight(name, side, unit_type, count, client_count, at.position)
        else:
            assert False

    def _add_radio_waypoint(self, group: FlyingGroup, position, altitude: int, airspeed: int = 600):
        point = group.add_waypoint(position, altitude, airspeed)
        point.alt_type = "RADIO"
        return point

    def _rtb_for(self, group: FlyingGroup, cp: ControlPoint, at: db.StartingPosition = None):
        if not at:
            at = cp.at
        position = at if isinstance(at, Point) else at.position

        last_waypoint = group.points[-1]
        if last_waypoint is not None:
            heading = position.heading_between_point(last_waypoint.position)
            tod_location = position.point_from_heading(heading, RTB_DISTANCE)
            self._add_radio_waypoint(group, tod_location, last_waypoint.alt)

        destination_waypoint = self._add_radio_waypoint(group, position, RTB_ALTITUDE)
        if isinstance(at, Airport):
            group.land_at(at)
        return destination_waypoint

    def _at_position(self, at) -> Point:
        if isinstance(at, Point):
            return at
        elif isinstance(at, ShipGroup):
            return at.position
        elif issubclass(at, Airport):
            return at.position
        else:
            assert False


    def _setup_custom_payload(self, flight, group:FlyingGroup):
        if flight.use_custom_loadout:

            logging.info("Custom loadout for flight : " + flight.__repr__())
            for p in group.units:
                p.pylons.clear()

            for key in flight.loadout.keys():
                if "Pylon" + key in flight.unit_type.__dict__.keys():
                    print(flight.loadout)
                    weapon_dict = flight.unit_type.__dict__["Pylon" + key].__dict__
                    if flight.loadout[key] in weapon_dict.keys():
                        weapon = weapon_dict[flight.loadout[key]]
                        group.load_pylon(weapon, int(key))
                else:
                    logging.warning("Pylon not found ! => Pylon" + key + " on " + str(flight.unit_type))


    def generate_flights(self, cp, country, flight_planner: FlightPlanner,
                         dynamic_runways: Dict[str, RunwayData]):
        # Clear pydcs parking slots
        if cp.airport is not None:
            logging.info("CLEARING SLOTS @ " + cp.airport.name)
            logging.info("===============")
            if cp.airport is not None:
                for ps in cp.airport.parking_slots:
                    logging.info("SLOT : " + str(ps.unit_id))
                    ps.unit_id = None
                logging.info("----------------")
            logging.info("===============")

        for flight in flight_planner.flights:

            if flight.client_count == 0 and self.game.position_culled(flight.from_cp.position):
                logging.info("Flight not generated : culled")
                continue
            logging.info("Generating flight : " + str(flight.unit_type))
            group = self.generate_planned_flight(cp, country, flight)
            self.setup_flight_group(group, flight, flight.flight_type,
                                    dynamic_runways)
            self.setup_group_activation_trigger(flight, group)


    def setup_group_activation_trigger(self, flight, group):
        if flight.scheduled_in > 0 and flight.client_count == 0:

            if flight.start_type != "In Flight" and flight.from_cp.cptype not in [ControlPointType.AIRCRAFT_CARRIER_GROUP, ControlPointType.LHA_GROUP]:
                group.late_activation = False
                group.uncontrolled = True

                activation_trigger = TriggerOnce(Event.NoEvent, "FlightStartTrigger" + str(group.id))
                activation_trigger.add_condition(TimeAfter(seconds=flight.scheduled_in * 60))
                if (flight.from_cp.cptype == ControlPointType.AIRBASE):
                    if flight.from_cp.captured:
                        activation_trigger.add_condition(
                            CoalitionHasAirdrome(self.game.get_player_coalition_id(), flight.from_cp.id))
                    else:
                        activation_trigger.add_condition(
                            CoalitionHasAirdrome(self.game.get_enemy_coalition_id(), flight.from_cp.id))

                if flight.flight_type == FlightType.INTERCEPTION:
                    self.setup_interceptor_triggers(group, flight, activation_trigger)

                group.add_trigger_action(StartCommand())
                activation_trigger.add_action(AITaskPush(group.id, len(group.tasks)))

                self.m.triggerrules.triggers.append(activation_trigger)
            else:
                group.late_activation = True
                activation_trigger = TriggerOnce(Event.NoEvent, "FlightLateActivationTrigger" + str(group.id))
                activation_trigger.add_condition(TimeAfter(seconds=flight.scheduled_in*60))

                if(flight.from_cp.cptype == ControlPointType.AIRBASE):
                    if flight.from_cp.captured:
                        activation_trigger.add_condition(CoalitionHasAirdrome(self.game.get_player_coalition_id(), flight.from_cp.id))
                    else:
                        activation_trigger.add_condition(CoalitionHasAirdrome(self.game.get_enemy_coalition_id(), flight.from_cp.id))

                if flight.flight_type == FlightType.INTERCEPTION:
                    self.setup_interceptor_triggers(group, flight, activation_trigger)

                activation_trigger.add_action(ActivateGroup(group.id))
                self.m.triggerrules.triggers.append(activation_trigger)

    def setup_interceptor_triggers(self, group, flight, activation_trigger):

        detection_zone = self.m.triggers.add_triggerzone(flight.from_cp.position, radius=25000, hidden=False, name="ITZ")
        if flight.from_cp.captured:
            activation_trigger.add_condition(PartOfCoalitionInZone(self.game.get_enemy_color(), detection_zone.id)) # TODO : support unit type in part of coalition
            activation_trigger.add_action(MessageToAll(String("WARNING : Enemy aircraft have been detected in the vicinity of " + flight.from_cp.name + ". Interceptors are taking off."), 20))
        else:
            activation_trigger.add_condition(PartOfCoalitionInZone(self.game.get_player_color(), detection_zone.id))
            activation_trigger.add_action(MessageToAll(String("WARNING : We have detected that enemy aircraft are scrambling for an interception on " + flight.from_cp.name + " airbase."), 20))

    def generate_planned_flight(self, cp, country, flight:Flight):
        try:
            if flight.client_count == 0 and self.game.settings.perf_ai_parking_start:
                flight.start_type = "Cold"

            if flight.start_type == "In Flight":
                group = self._generate_group(
                    name=namegen.next_unit_name(country, cp.id, flight.unit_type),
                    side=country,
                    unit_type=flight.unit_type,
                    count=flight.count,
                    client_count=0,
                    at=cp.position)
            else:
                st = StartType.Runway
                if flight.start_type == "Cold":
                    st = StartType.Cold
                elif flight.start_type == "Warm":
                    st = StartType.Warm

                if cp.cptype in [ControlPointType.AIRCRAFT_CARRIER_GROUP, ControlPointType.LHA_GROUP]:
                    group_name = cp.get_carrier_group_name()
                    group = self._generate_at_group(
                        name=namegen.next_unit_name(country, cp.id, flight.unit_type),
                        side=country,
                        unit_type=flight.unit_type,
                        count=flight.count,
                        client_count=0,
                        at=self.m.find_group(group_name),
                        start_type=st)
                else:
                    group = self._generate_at_airport(
                        name=namegen.next_unit_name(country, cp.id, flight.unit_type),
                        side=country,
                        unit_type=flight.unit_type,
                        count=flight.count,
                        client_count=0,
                        airport=cp.airport,
                        start_type=st)
        except Exception as e:
            # Generated when there is no place on Runway or on Parking Slots
            logging.error(e)
            logging.warning("No room on runway or parking slots. Starting from the air.")
            flight.start_type = "In Flight"
            group = self._generate_group(
                name=namegen.next_unit_name(country, cp.id, flight.unit_type),
                side=country,
                unit_type=flight.unit_type,
                count=flight.count,
                client_count=0,
                at=cp.position)
            group.points[0].alt = 1500

        flight.group = group
        return group


    def setup_flight_group(self, group, flight, flight_type,
                           dynamic_runways: Dict[str, RunwayData]):

        if flight_type in [FlightType.CAP, FlightType.BARCAP, FlightType.TARCAP, FlightType.INTERCEPTION]:
            group.task = CAP.name
            self._setup_group(group, CAP, flight, dynamic_runways)
            # group.points[0].tasks.clear()
            group.points[0].tasks.clear()
            group.points[0].tasks.append(EngageTargets(max_distance=nm_to_meter(50), targets=[Targets.All.Air]))
            # group.tasks.append(EngageTargets(max_distance=nm_to_meter(120), targets=[Targets.All.Air]))
            if flight.unit_type not in GUNFIGHTERS:
                group.points[0].tasks.append(OptRTBOnOutOfAmmo(OptRTBOnOutOfAmmo.Values.AAM))
            else:
                group.points[0].tasks.append(OptRTBOnOutOfAmmo(OptRTBOnOutOfAmmo.Values.Cannon))

        elif flight_type in [FlightType.CAS, FlightType.BAI]:
            group.task = CAS.name
            self._setup_group(group, CAS, flight, dynamic_runways)
            group.points[0].tasks.clear()
            group.points[0].tasks.append(EngageTargets(max_distance=nm_to_meter(10), targets=[Targets.All.GroundUnits.GroundVehicles]))
            group.points[0].tasks.append(OptReactOnThreat(OptReactOnThreat.Values.EvadeFire))
            group.points[0].tasks.append(OptROE(OptROE.Values.OpenFireWeaponFree))
            group.points[0].tasks.append(OptRTBOnOutOfAmmo(OptRTBOnOutOfAmmo.Values.Unguided))
            group.points[0].tasks.append(OptRestrictJettison(True))
        elif flight_type in [FlightType.SEAD, FlightType.DEAD]:
            group.task = SEAD.name
            self._setup_group(group, SEAD, flight, dynamic_runways)
            group.points[0].tasks.clear()
            group.points[0].tasks.append(NoTask())
            group.points[0].tasks.append(OptReactOnThreat(OptReactOnThreat.Values.EvadeFire))
            group.points[0].tasks.append(OptROE(OptROE.Values.OpenFire))
            group.points[0].tasks.append(OptRestrictJettison(True))
            group.points[0].tasks.append(OptRTBOnOutOfAmmo(OptRTBOnOutOfAmmo.Values.ASM))
        elif flight_type in [FlightType.STRIKE]:
            group.task = PinpointStrike.name
            self._setup_group(group, GroundAttack, flight, dynamic_runways)
            group.points[0].tasks.clear()
            group.points[0].tasks.append(OptReactOnThreat(OptReactOnThreat.Values.EvadeFire))
            group.points[0].tasks.append(OptROE(OptROE.Values.OpenFire))
            group.points[0].tasks.append(OptRestrictJettison(True))
        elif flight_type in [FlightType.ANTISHIP]:
            group.task = AntishipStrike.name
            self._setup_group(group, AntishipStrike, flight, dynamic_runways)
            group.points[0].tasks.clear()
            group.points[0].tasks.append(OptReactOnThreat(OptReactOnThreat.Values.EvadeFire))
            group.points[0].tasks.append(OptROE(OptROE.Values.OpenFire))
            group.points[0].tasks.append(OptRestrictJettison(True))

        group.points[0].tasks.append(OptRTBOnBingoFuel(True))
        group.points[0].tasks.append(OptRestrictAfterburner(True))

        if hasattr(flight.unit_type, 'eplrs'):
            if flight.unit_type.eplrs:
                group.points[0].tasks.append(EPLRS(group.id))

        for i, point in enumerate(flight.points):
            if not point.only_for_player or (point.only_for_player and flight.client_count > 0):
                pt = group.add_waypoint(Point(point.x, point.y), point.alt)
                if point.waypoint_type == FlightWaypointType.PATROL_TRACK:
                    action = ControlledTask(OrbitAction(altitude=pt.alt, pattern=OrbitAction.OrbitPattern.RaceTrack))
                    action.stop_after_duration(CAP_DURATION * 60)
                    #for tgt in point.targets:
                    #    if hasattr(tgt, "position"):
                    #        engagetgt = EngageTargetsInZone(tgt.position, radius=CAP_DEFAULT_ENGAGE_DISTANCE, targets=[Targets.All.Air])
                    #        pt.tasks.append(engagetgt)
                elif point.waypoint_type == FlightWaypointType.LANDING_POINT:
                    pt.type = "Land"
                elif point.waypoint_type == FlightWaypointType.INGRESS_STRIKE:

                    if group.units[0].unit_type == B_17G:
                        if len(point.targets) > 0:
                            bcenter = Point(0,0)
                            for j, t in enumerate(point.targets):
                                bcenter.x += t.position.x
                                bcenter.y += t.position.y
                            bcenter.x = bcenter.x / len(point.targets)
                            bcenter.y = bcenter.y / len(point.targets)
                            bombing = Bombing(bcenter)
                            bombing.params["expend"] = "All"
                            bombing.params["attackQtyLimit"] = False
                            bombing.params["directionEnabled"] = False
                            bombing.params["altitudeEnabled"] = False
                            bombing.params["weaponType"] = 2032
                            bombing.params["groupAttack"] = True
                            pt.tasks.append(bombing)
                    else:
                        for j, t in enumerate(point.targets):
                            print(t.position)
                            pt.tasks.append(Bombing(t.position))
                            if group.units[0].unit_type == JF_17 and j < 4:
                                group.add_nav_target_point(t.position, "PP" + str(j + 1))
                            if group.units[0].unit_type == F_14B and j == 0:
                                group.add_nav_target_point(t.position, "ST")
                            if group.units[0].unit_type == AJS37 and j < 9:
                                group.add_nav_target_point(t.position, "M" + str(j + 1))
                elif point.waypoint_type == FlightWaypointType.INGRESS_SEAD:

                    tgroup = self.m.find_group(point.targetGroup.group_identifier)
                    if tgroup is not None:
                        task = AttackGroup(tgroup.id)
                        task.params["expend"] = "All"
                        task.params["attackQtyLimit"] = False
                        task.params["directionEnabled"] = False
                        task.params["altitudeEnabled"] = False
                        task.params["weaponType"] = 268402702 # Guided Weapons
                        task.params["groupAttack"] = True
                        pt.tasks.append(task)

                    for j, t in enumerate(point.targets):
                        if group.units[0].unit_type == JF_17 and j < 4:
                            group.add_nav_target_point(t.position, "PP" + str(j + 1))
                        if group.units[0].unit_type == F_14B and j == 0:
                            group.add_nav_target_point(t.position, "ST")
                        if group.units[0].unit_type == AJS37 and j < 9:
                            group.add_nav_target_point(t.position, "M" + str(j + 1))

                if pt is not None:
                    pt.alt_type = point.alt_type
                    pt.name = String(point.name)

        self._setup_custom_payload(flight, group)
