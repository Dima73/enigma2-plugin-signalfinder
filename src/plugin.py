from __future__ import print_function
from . import _
from Screens.Screen import Screen
from Plugins.Plugin import PluginDescriptor
from Components.config import config, ConfigSubsection, ConfigSelection, \
	ConfigYesNo, ConfigInteger, getConfigListEntry, ConfigEnableDisable
from Components.ActionMap import NumberActionMap, ActionMap
from Components.ConfigList import ConfigListScreen
from Components.NimManager import nimmanager, getConfigSatlist
from Components.Label import Label
from Screens.Console import Console
from Screens.MessageBox import MessageBox
from enigma import eTimer, eDVBFrontendParametersSatellite, eDVBFrontendParametersTerrestrial, eComponentScan, eDVBResourceManager, eDVBDB, getDesktop
from Components.Sources.FrontendStatus import FrontendStatus
from Components.TuneTest import Tuner
from Components.MenuList import MenuList
from Screens.ChoiceBox import ChoiceBox
from Screens.ServiceScan import ServiceScan
import os
import re
import re

config.misc.direct_tuner = ConfigYesNo(False)

plugin_version = "3.2"

def getKeepKeywords():
    keywords = []
    # Path to the text file in the plugin directory
    keywords_file = os.path.join(os.path.dirname(__file__), "keep_keywords.txt")
    if os.path.exists(keywords_file):
        with open(keywords_file, "r") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    keywords.append(line)
    if not keywords:
        # Defaults if file is empty or missing
        keywords = ["feed", "sng", "enc", "dsng"]
    return keywords

def collectKeepServicesSnapshot(orb_filter=None):
	# orb_filter is the integer orbital position (e.g. 192 for 19.2E, -70 for 7.0W)
	lamedb_path = "/etc/enigma2/lamedb"
	if not os.path.exists(lamedb_path):
		return None
	try:
		with open(lamedb_path, "r") as fd:
			lines = fd.readlines()
	except:
		return None

	t_idx, t_end, s_idx, s_end = parseLamedb(lines)
	if min(t_idx, t_end, s_idx, s_end) < 0:
		return None

	keywords = getKeepKeywords()
	relevant_tp_keys = set()
	keep_transponders = {}
	transponders_data = lines[t_idx + 1:t_end]
	
	idx = 0
	while idx < len(transponders_data):
		line = transponders_data[idx].strip()
		parts = line.split(":")
		if len(parts) >= 3:
			try:
				# CORRECT ENIGMA2 LOGIC: Orbital position is in the top 16 bits
				ns_val = int(parts[0], 16)
				tp_orb = ns_val >> 16
				if tp_orb > 1800: # Handle West positions
					tp_orb -= 3600
					
				# Only snapshot if it matches our current satellite filter
				if orb_filter is None or int(tp_orb) == int(orb_filter):
					keep_transponders[line] = [transponders_data[idx], transponders_data[idx+1], transponders_data[idx+2]]
					relevant_tp_keys.add(line)
			except: pass
		idx += 3

	keep_services = []
	services_data = lines[s_idx + 1:s_end]
	idx = 0
	while idx + 2 < len(services_data):
		ref = services_data[idx].strip()
		name = services_data[idx + 1].strip()
		tp_key = getTpKeyFromServiceRef(ref)
		
		# Only keep services belonging to the filtered transponders
		if tp_key in relevant_tp_keys:
			if any(re.search(kw, name, re.IGNORECASE) for kw in keywords):
				keep_services.append([services_data[idx], services_data[idx + 1], services_data[idx + 2]])
		idx += 3

	return {"transponders": keep_transponders, "services": keep_services}

def getDesktopSize():
	s = getDesktop(0).size()
	return (s.width(), s.height())

def isHD():
	desktopSize = getDesktopSize()
	return desktopSize[0] == 1280

multistream = hasattr(eDVBFrontendParametersSatellite, "PLS_Root")
t2mi = hasattr(eDVBFrontendParametersSatellite, "No_T2MI_PLP_Id") and hasattr(eDVBFrontendParametersSatellite, "T2MI_Default_Pid")

loadScript = "/usr/lib/enigma2/python/Plugins/SystemPlugins/Signalfinder/update-plugin.sh"
DEFAULT_FEED_NAME_RE = re.compile("(feed|sng|enc)", re.IGNORECASE)


def normalizeFeedNamespacesInLamedb():
	lamedb_path = "/etc/enigma2/lamedb"
	if not os.path.exists(lamedb_path):
		return
	try:
		fd = open(lamedb_path, "r")
		try:
			lines = fd.readlines()
		finally:
			fd.close()
	except Exception as err:
		print("[Signalfinder] Failed to read lamedb for namespace normalization: %s" % err)
		return

	transponders_idx = services_idx = transponders_end_idx = services_end_idx = -1
	for i, line in enumerate(lines):
		token = line.strip()
		if token == "transponders" and transponders_idx == -1:
			transponders_idx = i
		elif token == "services" and services_idx == -1:
			services_idx = i
		elif token == "end" and transponders_idx != -1 and transponders_end_idx == -1:
			transponders_end_idx = i
		elif token == "end" and services_idx != -1 and i > services_idx:
			services_end_idx = i
			break
	if min(transponders_idx, services_idx, transponders_end_idx, services_end_idx) < 0:
		return

	trans_data = lines[transponders_idx + 1:transponders_end_idx]
	services_data = lines[services_idx + 1:services_end_idx]
	namespace_count = {}
	for line in trans_data:
		key = line.strip()
		parts = key.split(":")
		if len(parts) == 3:
			ns = parts[0]
			try:
				orb = int(ns, 16) & 0xFFFF
			except Exception:
				continue
			namespace_count.setdefault(orb, {})
			namespace_count[orb][ns] = namespace_count[orb].get(ns, 0) + 1

	preferred_ns = {}
	for orb, counts in namespace_count.items():
		preferred_ns[orb] = sorted(counts.items(), key=lambda x: x[1], reverse=True)[0][0]

	key_replace = {}
	idx = 0
	while idx + 2 < len(services_data):
		ref_line = services_data[idx].strip()
		name_line = services_data[idx + 1].strip()
		if DEFAULT_FEED_NAME_RE.search(name_line):
			ref_tokens = ref_line.split(":")
			if len(ref_tokens) >= 7 and ref_tokens[1] == "0":
				ns = ref_tokens[6]
				tsid = ref_tokens[4]
				onid = ref_tokens[5]
				try:
					orb = int(ns, 16) & 0xFFFF
				except Exception:
					idx += 3
					continue
				target_ns = preferred_ns.get(orb, ns)
				if target_ns != ns:
					key_replace["%s:%s:%s" % (ns, tsid, onid)] = "%s:%s:%s" % (target_ns, tsid, onid)
					ref_tokens[6] = target_ns
					services_data[idx] = ":".join(ref_tokens) + "\n"
		idx += 3

	if not key_replace:
		return

	for i, line in enumerate(trans_data):
		key = line.strip()
		if key in key_replace:
			trans_data[i] = key_replace[key] + "\n"

	new_lines = lines[:transponders_idx + 1] + trans_data + lines[transponders_end_idx:services_idx + 1] + services_data + lines[services_end_idx:]
	tmp_path = lamedb_path + ".tmp"
	try:
		fd = open(tmp_path, "w")
		try:
			fd.writelines(new_lines)
		finally:
			fd.close()
		os.rename(tmp_path, lamedb_path)
	except Exception as err:
		print("[Signalfinder] Failed to write normalized lamedb: %s" % err)
		try:
			if os.path.exists(tmp_path):
				os.remove(tmp_path)
		except Exception:
			pass
PLUGIN_DIR = os.path.dirname(os.path.realpath(__file__))
FEED_KEYWORDS_FILES_GLOB = ".txt"
DEFAULT_KEEP_FEED_KEYWORDS = ["feed", "sng", "enc"]


def loadFeedKeepKeywordRegex():
	patterns = []
	custom_keywords_loaded = False
	try:
		file_list = sorted([x for x in os.listdir(PLUGIN_DIR) if x.lower().endswith(FEED_KEYWORDS_FILES_GLOB)])
	except Exception as err:
		print("[Signalfinder] Failed to read feed keywords list: %s" % err)
		file_list = []
	for file_name in file_list:
		file_path = os.path.join(PLUGIN_DIR, file_name)
		try:
			fd = open(file_path, "r")
			try:
				for line in fd:
					keyword = line.strip()
					if not keyword or keyword.startswith("#"):
						continue
					try:
						patterns.append(re.compile(keyword, re.IGNORECASE))
						custom_keywords_loaded = True
					except re.error as regex_err:
						print("[Signalfinder] Invalid feed keyword regex '%s' in %s: %s" % (keyword, file_name, regex_err))
			finally:
				fd.close()
		except Exception as err:
			print("[Signalfinder] Failed to read feed keyword file %s: %s" % (file_name, err))
	if not custom_keywords_loaded:
		for keyword in DEFAULT_KEEP_FEED_KEYWORDS:
			patterns.append(re.compile(keyword, re.IGNORECASE))
	return patterns


def parseLamedb(lines):
	transponders_idx = -1
	transponders_end_idx = -1
	services_idx = -1
	services_end_idx = -1
	for i, line in enumerate(lines):
		token = line.strip()
		if transponders_idx == -1 and token == "transponders":
			transponders_idx = i
			continue
		if transponders_idx != -1 and transponders_end_idx == -1 and token == "end":
			transponders_end_idx = i
			continue
		if services_idx == -1 and token == "services":
			services_idx = i
			continue
		if services_idx != -1 and token == "end":
			services_end_idx = i
			break
	return transponders_idx, transponders_end_idx, services_idx, services_end_idx


def getTpKeyFromServiceRef(ref_line):
	ref_tokens = ref_line.strip().split(":")
	if len(ref_tokens) >= 7 and ref_tokens[1] == "0":
		return "%s:%s:%s" % (ref_tokens[6], ref_tokens[4], ref_tokens[5])
	if len(ref_tokens) >= 4:
		return "%s:%s:%s" % (ref_tokens[1], ref_tokens[2], ref_tokens[3])
	return None


def replaceServiceRefTpKey(ref_line, tp_key):
	ref_tokens = ref_line.strip().split(":")
	tp_tokens = tp_key.split(":")
	if len(ref_tokens) >= 7 and ref_tokens[1] == "0" and len(tp_tokens) == 3:
		ref_tokens[6] = tp_tokens[0]
		ref_tokens[4] = tp_tokens[1]
		ref_tokens[5] = tp_tokens[2]
		return ":".join(ref_tokens) + "\n"
	if len(ref_tokens) >= 4 and len(tp_tokens) == 3:
		ref_tokens[1] = tp_tokens[0]
		ref_tokens[2] = tp_tokens[1]
		ref_tokens[3] = tp_tokens[2]
		return ":".join(ref_tokens) + "\n"
	return ref_line


def parseTransponderBlocks(transponders_data):
	transponders = {}
	signatures = {}
	tp_idx = 0
	while tp_idx < len(transponders_data):
		key_line = transponders_data[tp_idx]
		key = key_line.strip()
		block = [key_line]
		tp_idx += 1
		while tp_idx < len(transponders_data):
			block.append(transponders_data[tp_idx])
			if transponders_data[tp_idx].strip() == "/":
				tp_idx += 1
				break
			tp_idx += 1
		if key:
			transponders[key] = block
			signature = tuple([x.strip() for x in block[1:]])
			signatures[signature] = key
	return transponders, signatures

def appendSnapshotServicesToLamedb(snapshot):
	if not snapshot or (not snapshot.get("services")):
		return
	lamedb_path = "/etc/enigma2/lamedb"
	if not os.path.exists(lamedb_path):
		return
	try:
		with open(lamedb_path, "r") as fd:
			lines = fd.readlines()
	except:
		return

	t_idx, t_end, s_idx, s_end = parseLamedb(lines)
	if min(t_idx, t_end, s_idx, s_end) < 0:
		return

	# 1. Identify the 'Active' Namespace the scanner just used
	# We look at the first 4 chars (e.g., '0dbf') to match the satellite
	existing_tp, existing_sig = parseTransponderBlocks(lines[t_idx + 1:t_end])
	active_namespaces = {}
	for key in existing_tp.keys():
		parts = key.split(":")
		if len(parts) == 3:
			ns_hex = parts[0].zfill(8)
			sat_id = ns_hex[:4] # Grab '0dbf' or 'ffaf'
			if sat_id not in active_namespaces:
				active_namespaces[sat_id] = parts[0]

	tp_key_map = {}
	new_tp_blocks = []
	existing_keys = set(existing_tp.keys())

	# 2. Force Snapshot Transponders to use the New Scan's Namespace
	for old_key, block in snapshot.get("transponders", {}).items():
		target_key = old_key
		parts = old_key.split(":")
		if len(parts) == 3:
			old_ns_hex = parts[0].zfill(8)
			old_sat_id = old_ns_hex[:4]
			
			# If the scanner used a different namespace for this satellite id
			new_ns = active_namespaces.get(old_sat_id)
			if new_ns and new_ns != parts[0]:
				target_key = "%s:%s:%s" % (new_ns, parts[1], parts[2])
				# Update the block data line
				block[0] = target_key + "\n"

		tp_key_map[old_key] = target_key
		if target_key not in existing_keys:
			new_tp_blocks.extend(block)
			existing_keys.add(target_key)

	# 3. Restore Services using the mapped Transponder Keys
	new_service_blocks = []
	existing_refs = set()
	idx = 0
	s_data = lines[s_idx + 1:s_end]
	while idx + 2 < len(s_data):
		existing_refs.add(s_data[idx].strip())
		idx += 3

	for s_block in snapshot.get("services", []):
		old_tp = getTpKeyFromServiceRef(s_block[0])
		target_tp = tp_key_map.get(old_tp, old_tp)
		
		ref_line = s_block[0]
		if old_tp != target_tp:
			ref_line = replaceServiceRefTpKey(ref_line, target_tp)
		
		if ref_line.strip() not in existing_refs:
			new_service_blocks.extend([ref_line, s_block[1], s_block[2]])

	# 4. Write back to lamedb and reload
	if new_tp_blocks or new_service_blocks:
		new_lines = lines[:t_end] + new_tp_blocks + lines[t_end:s_end] + new_service_blocks + lines[s_end:]
		try:
			with open(lamedb_path, "w") as f:
				f.writelines(new_lines)
			# Force reload
			db = eDVBDB.getInstance()
			db.reloadServicelist()
			db.reloadBouquets()
		except:
			pass

VIASATUKR = [(12288000, 0)] #Amos 4w
VIASAT = [(11265000, 0), (11265000, 1), (11305000, 0), (11305000, 1), (11345000, 1), (11345000, 0), (11385000, 1), (11727000, 0), (11785000, 1), (11804000, 0), (11823000, 1), (11843000, 0), (11862000, 1), (11881000, 0), (11900000, 1), (11919000, 0), (11938000, 1), (11958000, 0), (11977000, 1), (11996000, 0), (12015000, 1), (12034000, 0), (12054000, 1), (12092000, 1), (12245000, 1), (12380000, 0), (12437000, 1), (12476000, 1), (12608000, 0), (12637000, 0)]
XTRATV = [(12111000, 1), (12149000, 1)]
VIASATLATVIJA = [(11265000, 0), (11265000, 1), (11305000, 1), (11345000, 1), (11727000, 0), (11785000, 1), (11804000, 0), (11823000, 1), (11843000, 0), (11862000, 1), (11881000, 0), (11900000, 1), (11919000, 0), (11938000, 1), (11958000, 0), (11977000, 1), (11996000, 0), (12015000, 1), (12034000, 0), (12437000, 1), (12608000, 0)]
NTVPLUS = [(11785000, 1), (11823000, 1), (11862000, 1), (11900000, 1), (11938000, 1), (11977000, 1), (12015000, 1), (12092000, 1), (12130000, 1), (12207000, 1), (12245000, 1), (12265000, 0), (12284000, 1), (12322000, 1), (12341000, 0), (12399000, 1), (12437000, 1)]
TRIKOLOR = [(11727000, 0), (11747000, 1), (11766000, 0), (11804000, 0), (11843000, 0), (11881000, 0), (11919000, 0), (11958000, 0), (11996000, 0), (12034000, 0), (12054000, 1), (12073000, 0), (12111000, 0), (12149000, 0), (12169000, 1), (12190000, 0), (12226000, 0), (12303000, 0), (12360000, 1), (12380000, 0), (12418000, 0), (12456000, 0), (12476000, 0)]
NTVPLUS_VOSTOK = [(12054000, 1), (12092000, 1), (12169000, 1), (12245000, 1), (12322000, 1), (12399000, 1), (12437000, 1), (12476000, 1)]
TRIKOLOR_SIBIR = [(11881000, 0), (11919000, 0), (11958000, 0), (11996000, 0), (12034000, 0), (12073000, 0), (12111000, 0), (12149000, 0), (12188000, 0), (12226000, 0), (12265000, 0), (12303000, 0), (12341000, 0)]
OTAUTV = [(10720000, 0), (10762000, 0), (10804000, 0), (10887000, 0)]
RADUGA = [(11473000, 1), (11559000, 1), (11793000, 1)]
MTSTV = [(11740000, 1), (11800000, 1), (11860000, 1), (11920000, 1), (11980000, 1), (12040000, 1), (12100000, 1), (12160000, 1)]
KONTINENT = [(11720000, 0), (11760000, 0), (11800000, 0), (11840000, 0), (11880000, 0), (11872000, 0), (11920000, 0), (11960000, 0), (12000000, 0), (12040000, 0), (12080000, 0), (12120000, 0), (12160000, 0), (12560000, 1), (12600000, 1), (12640000, 1)]
NTVPLUS_DAL_VOSTOK = [(12226000, 0), (12245000, 1), (12303000, 0), (12322000, 1), (12380000, 0), (12399000, 1), (12456000, 0), (12476000, 1)]
KONTINENT_DAL_VOSTOK = [(10981000, 1), (11387000, 1), (11530000, 0), (11557000, 0), (11665000, 1)]


class TranspondersList(Screen):
	skin = """
	<screen position="center,center" size="400,450" title="Transponders list" >
		<widget name="menu" position="10,10" size="385,400" scrollbarMode="showOnDemand" />
		<ePixmap pixmap="skin_default/div-h.png" position="0,415" zPosition="10" size="400,2" transparent="1" alphatest="on" />
		<widget name="introduction" position="10,420" size="385,25" font="Regular;20" halign="center" valign="center" />
	</screen>"""

	def __init__(self, session, list=None, sat=None, args=None):
		Screen.__init__(self, session)
		self.session = session
		self.menu = args
		self.list = list
		self.sat = sat
		if self.sat is not None:
			self.setup_title = _("Transponders list: %s") % self.sat
		else:
			self.setup_title = _("Transponders list")
		self["menu"] = MenuList(self.list)
		self["introduction"] = Label(_("Press OK to toggle the selection"))
		self["actions"] = ActionMap(["OkCancelActions"], {"ok": self.run, "cancel": self.cansel}, -1)
		self.onLayoutFinish.append(self.setCustomTitle)

	def setCustomTitle(self):
		self.setTitle(self.setup_title)

	def run(self):
		try:
			returnValue = self["menu"].l.getCurrentSelection()[1]
			if returnValue is not None:
				self.close(returnValue)
		except:
			pass

	def cansel(self):
		self.close(None)


class SignalFinderMultistreamT2MI(ConfigListScreen, Screen):
	if isHD():
		skin = """
			<screen position="center,center" size="1200,635" title="Signal finder" >
				<widget name="pos" position="10,10" size="210,25" font="Regular;22" halign="right" transparent="1" />
				<widget name="status" position="230,10" size="370,25" font="Regular;22" halign="left" foregroundColor="#f8f711" transparent="1" />
				<widget source="Frontend" render="Progress" pixmap="/usr/lib/enigma2/python/Plugins/SystemPlugins/Signalfinder/image/bar_big.png" position="140,40" size="1000,50" borderColor="#00808888">
					<convert type="FrontendInfo">SNR</convert>
				</widget>
				<eLabel text="SNR:" position="145,50" size="100,35" backgroundColor="#00666666" transparent="1" font="Regular;35" />
				<widget source="Frontend" render="Label" position="910,50" size="225,35" halign="right" transparent="1" font="Regular;35">
					<convert type="FrontendInfo">SNR</convert>
				</widget>
				<widget source="Frontend" render="Progress" pixmap="/usr/lib/enigma2/python/Plugins/SystemPlugins/Signalfinder/image/bar_agc.png" position="140,100" size="1000,50" borderColor="#00808888">
					<convert type="FrontendInfo">AGC</convert>
				</widget>
				<eLabel text="AGC:" position="145,110" size="100,35" backgroundColor="#00666666" transparent="1" font="Regular;35" />
				<widget source="Frontend" render="Label" position="910,110" size="225,35" halign="right" transparent="1" font="Regular;35">
					<convert type="FrontendInfo">AGC</convert>
				</widget>
				<widget source="Frontend" render="Progress" pixmap="/usr/lib/enigma2/python/Plugins/SystemPlugins/Signalfinder/image/bar_ber.png" position="140,160" size="1000,50" borderColor="#00808888">
					<convert type="FrontendInfo">BER</convert>
				</widget>
				<eLabel text="BER:" position="145,170" size="100,35" backgroundColor="#00666666"  transparent="1" font="Regular;35" />
				<widget source="Frontend" render="Label" position="910,170" size="225,35" halign="right" transparent="1" font="Regular;35">
					<convert type="FrontendInfo">BER</convert>
				</widget>
				<eLabel text="SNR:" position="140,225" size="120,20" backgroundColor="#00666666" transparent="1" zPosition="5" font="Regular;18" />
				<widget source="Frontend" render="Label" position="140,245" size="310,75" font="Regular;72" halign="left" backgroundColor="#00666666" transparent="1">
					<convert type="FrontendInfo">SNRdB</convert>
				</widget>
				<eLabel text="AGC:" position="140,325" size="120,20" backgroundColor="#00666666" transparent="1" zPosition="5" font="Regular;18" />
				<widget source="Frontend" render="Label" position="140,345" size="310,75" backgroundColor="#00666666" transparent="1" font="Regular;72" halign="left">
					<convert type="FrontendInfo">AGC</convert>
				</widget>
				<eLabel text="BER:" position="140,425" size="120,20" backgroundColor="#00666666" transparent="1" zPosition="5" font="Regular;18" />
				<widget source="Frontend" render="Label" position="140,445" size="310,75" font="Regular;72" halign="left" backgroundColor="#00666666" transparent="1">
					<convert type="FrontendInfo">BER</convert>
				</widget>
				<widget text="LOCK" source="Frontend" render="FixedLabel" position="140,525" size="310,90" font="Regular;72" halign="left" foregroundColor="#00389416" backgroundColor="#00666666" transparent="1" >
					<convert type="FrontendInfo">LOCK</convert>
					<convert type="ConditionalShowHide" />
				</widget>
				<widget name="config" position="420,225" size="720,396" transparent="1" scrollbarMode="showOnDemand" />
				<widget name="introduction" position="540,10" size="600,30"  zPosition="1" transparent="1" foregroundColor="#f8f711" font="Regular;22" />
				<widget name="Cancel" position="200,610" size="250,28" foregroundColor="#00ff2525" zPosition="1" transparent="1" font="Regular;24" />
				<widget name="Scan" position="750,610" size="250,28" foregroundColor="#00389416" zPosition="1" transparent="1" font="Regular;24" />
			</screen>"""
	else:
		skin = """
			<screen position="center,center" size="1020,900" title="Signal finder">
				<widget name="pos" position="20,5" size="300,33" font="Regular; 26" halign="right" transparent="1" />
				<widget name="status" position="325,5" size="650,33" font="Regular; 26" halign="left" foregroundColor="#f8f711" transparent="1" />
				<widget source="Frontend" render="Label" position="255,45" zPosition="2" size="130,125" font="Regular; 35" halign="center" valign="center" transparent="1">
					<convert type="FrontendInfo">SNRdB</convert>
				</widget>
				<eLabel name="snr" text="SNR:" position="455,45" size="100,35" font="Regular; 30" halign="right" transparent="1" />
				<widget source="Frontend" render="Progress" position="565,55" size="278,30" pixmap="skin_default/bar_snr.png" borderColor="uncccccc">
					<convert type="FrontendInfo">SNR</convert>
				</widget>
				<widget source="Frontend" render="Label" position="850,45" size="80,35" font="Regular; 26">
					<convert type="FrontendInfo">SNR</convert>
				</widget>
				<eLabel name="lock" text="LOCK:" position="115,105" size="100,44" font="Regular; 30" halign="right" transparent="1" />
				<widget source="Frontend" render="Pixmap" pixmap="skin_default/icons/lock_on.png" position="150,55" zPosition="1" size="38,31" alphatest="on">
					<convert type="FrontendInfo">LOCK</convert>
					<convert type="ConditionalShowHide" />
				</widget>
				<widget source="Frontend" render="Pixmap" pixmap="skin_default/icons/lock_off.png" position="150,55" zPosition="1" size="38,31" alphatest="on">
					<convert type="FrontendInfo">LOCK</convert>
					<convert type="ConditionalShowHide">Invert</convert>
				</widget>
				<eLabel name="agc" text="AGC:" position="455,90" size="100,35" font="Regular; 30" halign="right" transparent="1" />
				<widget source="Frontend" render="Progress" position="565,100" size="278,30" pixmap="skin_default/bar_snr.png" borderColor="uncccccc">
					<convert type="FrontendInfo">AGC</convert>
				</widget>
				<widget source="Frontend" render="Label" position="850,90" size="80,35" font="Regular; 26">
					<convert type="FrontendInfo">AGC</convert>
				</widget>
				<eLabel name="ber" text="BER:" position="455,135" size="100,35" font="Regular; 30" halign="right" transparent="1" />
				<widget source="Frontend" render="Progress" position="565,145" size="278,30" pixmap="skin_default/bar_ber.png" borderColor="uncccccc">
					<convert type="FrontendInfo">BER</convert>
				</widget>
				<widget source="Frontend" render="Label" position="850,135" size="80,35" font="Regular; 26">
					<convert type="FrontendInfo">BER</convert>
				</widget>
				<widget name="config" position="20,195" size="970,550" scrollbarMode="showOnDemand" transparent="1" itemHeight="50" font="Regular; 38" />
				<widget name="introduction" position="20,763" size="970,48" font="Regular; 36" halign="center" foregroundColor="#f8f711" valign="center" />
				<widget name="Cancel" position="85,830" size="250,38" foregroundColor="#ff2525" zPosition="1" transparent="1" font="Regular; 28" halign="center" />
				<widget name="Scan" position="710,830" size="250,38" foregroundColor="green" zPosition="1" transparent="1" font="Regular; 28" halign="center" />
			</screen>"""

	def __init__(self, session):
		self.skin = SignalFinderMultistreamT2MI.skin
		Screen.__init__(self, session)
		self.initcomplete = False
		try:
			self.session.postScanService = self.session.nav.getCurrentlyPlayingServiceOrGroup()
		except:
			self.session.postScanService = self.session.nav.getCurrentlyPlayingServiceReference()
		nim = self.getNimvalue()
		self.updateSatList()
		self.service = session and session.nav.getCurrentService()
		self.getCurrentTuner = None
		self.stop_service = False
		feinfo = None
		self.tuner = None
		self.DLG = None
		self.frontendData = None
		try:
			self.rotor_pos = config.usage.showdish.value and config.misc.lastrotorposition.value != 9999
		except:
			self.rotor_pos = False
		self["pos"] = Label(_("Current position:"))
		self["status"] = Label("")
		self["Cancel"] = Label(_("Cancel"))
		self["Scan"] = Label(_("Scan"))
		self.tuneTimer = eTimer()
		self.tuneTimer.callback.append(self.updateTuneStatus)
		self.relock_fail_count = 0
		self.relock_restart_threshold = 1
		self.relock_restart_pending = False
		need_sat = False
		if self.service is not None:
			feinfo = self.service.frontendInfo()
			self.frontendData = feinfo and feinfo.getAll(True)
			if self.frontendData:
				type = self.frontendData.get("tuner_type", "UNKNOWN")
				if type == "DVB-S":
					orbital_position = self.frontendData.get("orbital_position", 0)
					if not self.scan_nims.value == "" and len(nim) > 0 and orbital_position != 0:
						tuner = self.frontendData.get("tuner_number", 0)
						get_tuner = False
						for x in nim:
							if int(x[0]) != tuner:
								if not config.misc.direct_tuner.value:
									satList = nimmanager.getSatListForNim(int(x[0]))
									for sat in satList:
										if sat[0] == orbital_position:
											need_sat = True
											self.scan_nims.setValue(x[0])
							else:
								satList = nimmanager.getSatListForNim(int(x[0]))
								for sat in satList:
									if sat[0] == orbital_position:
										get_tuner = True
						if not need_sat and get_tuner:
							need_sat = True
							self.scan_nims.setValue(str(tuner))
		if self.frontendData and not need_sat:
			self.frontendData = None
		if multistream:
			self.createConfig(self.frontendData)
		del feinfo
		del self.service
		self["Frontend"] = FrontendStatus(frontend_source=lambda: self.frontend, update_interval=500)
		self["actions"] = ActionMap(["SetupActions", "MenuActions"],
		{
			"save": self.keyGo,
			"ok": self.keyOK,
			"cancel": self.keyCancel,
			"menu": self.extaMenu,
		}, -2)
		self.list = []
		self.tpslist = []
		self.tpslist_idx = 0
		self["introduction"] = Label("")

		ConfigListScreen.__init__(self, self.list)
		self.feid = None
		if multistream:
			if not self.scan_nims.value == "":
				self.createSetup(self.frontendData)
				self.feid = int(self.scan_nims.value)
				orbpos = "??"
				if len(self.satList) > self.feid and len(self.scan_satselection) > self.feid and len(self.satList[self.feid]):
					orbpos = self.OrbToStr(self.satList[self.feid][self.scan_satselection[self.feid].index][0])
				status_text = orbpos + ": " + str(self.scan_sat.frequency.value) + " " + self.PolToStr(self.scan_sat.polarization.value)
				self["status"].setText(status_text)
		self["config"].onSelectionChanged.append(self.textHelp)
		self.initcomplete = self.feid != None
		self.setTitle(_("Signal finder for DVB-S(S2) tuners") + ": " + plugin_version)
		self.onShow.append(self.initFrontend)

	def extaMenu(self):
		menu_text = _("Set free tuner")
		if not config.misc.direct_tuner.value:
			menu_text = _("Set direct tuner")
		menu = [(menu_text, "setdirect"), (_("Update plugin"), "update")]

		def extraAction(choice):
			if choice is not None:
				if choice[1] == "setdirect":
					self.setDirectTuners()
				elif choice[1] == "update":
					cmd = "chmod 755 %s && %s update" % (loadScript, loadScript)
					text = _("Update plugin")
					self.session.open(Console, text, [cmd])
		self.session.openWithCallback(extraAction, ChoiceBox, title=_("Select action:"), list=menu)

	def setDirectTuners(self):
		text = _("Set free tuner") + "?"
		if not config.misc.direct_tuner.value:
			text = _("Set direct tuner") + "?"
		self.session.openWithCallback(self.setDirectTunersCallback, MessageBox, text, MessageBox.TYPE_YESNO)

	def setDirectTunersCallback(self, answer):
		if answer:
			config.misc.direct_tuner.value = not config.misc.direct_tuner.value
			config.misc.direct_tuner.save()

	def openFrontend(self):
		res_mgr = eDVBResourceManager.getInstance()
		if res_mgr and self.feid != None:
			self.raw_channel = res_mgr.allocateRawChannel(self.feid)
			if self.raw_channel:
				self.frontend = self.raw_channel.getFrontend()
				if self.frontend:
					return True
		return False

	def Exit(self, answer=None):
		self.close()

	def initFrontend(self):
		self.frontend = None
		if not multistream:
			if self.DLG is None:
				self.DLG = self.session.openWithCallback(self.Exit, MessageBox, _("This image not support multistream!"), MessageBox.TYPE_ERROR)
			return
		if not self.openFrontend():
			stop_service = True
			if self.frontendData and not self.stop_service:
				self.getCurrentTuner = self.frontendData and self.frontendData.get("tuner_number", None)
				if self.session.postScanService and self.getCurrentTuner is not None:
					if self.feid is not None and self.feid != self.getCurrentTuner:
						stop_service = False
						for n in nimmanager.nim_slots:
							if hasattr(n, 'config_mode') and n.config_mode in ("loopthrough", "satposdepends"):
								root_id = nimmanager.sec.getRoot(n.slot_id, int(n.config.connectedTo.value))
								if n.config.connectedTo.value and int(n.config.connectedTo.value) == self.feid:
									stop_service = True
							elif hasattr(n, 'config') and hasattr(n.config, 'dvbs') and hasattr(n.config.dvbs, 'configMode') and n.config.dvbs.configMode.value in ("loopthrough", "satposdepends"):
								root_id = nimmanager.sec.getRoot(n.slot_id, int(n.config.dvbs.connectedTo.value))
								if n.config.dvbs.connectedTo.value and int(n.config.dvbs.connectedTo.value) == self.feid:
									stop_service = True
			if self.session.postScanService and stop_service:
				self.session.nav.stopService()
				self.stop_service = True
			if not self.openFrontend():
				if self.session.pipshown:
					if hasattr(self.session, 'infobar'):
						try:
							if self.session.infobar.servicelist and self.session.infobar.servicelist.dopipzap:
								self.session.infobar.servicelist.togglePipzap()
						except:
							pass
					if hasattr(self.session, 'pip'):
						del self.session.pip
					self.session.pipshown = False
				if not self.openFrontend():
					self.deInitFrontend()
					text = _("Sorry, this tuner is in use.")
					if self.session.nav.getRecordings():
						text += "\n"
						text += _("Maybe the reason that recording is currently running.")
					if self.DLG is None:
						self.DLG = self.session.open(MessageBox, text, MessageBox.TYPE_ERROR)
		self.tuner = Tuner(self.frontend)
		self.retune(None)

	def restartFrontendForRelock(self):
		if self.relock_restart_pending:
			return
		self.relock_restart_pending = True
		self.relock_fail_count = 0
		self.tuneTimer.stop()
		self["status"].setText(_("restarting tuner..."))
		try:
			self.session.nav.stopService()
		except:
			pass
		self.stop_service = True
		self.deInitFrontend()
		self.initFrontend()
		self.relock_restart_pending = False

	def deInitFrontend(self):
		if self.DLG:
			try:
				del self.DLG
				self.DLG = None
			except:
				pass
		self.frontend = None
		self.tuner = None
		if hasattr(self, 'raw_channel'):
			del self.raw_channel

	def textHelp(self):
		self["introduction"].setText("")
		cur = self["config"].getCurrent()
		if self.scan_type.value == "predefined_transponder" and self.transpondersEntry is not None and cur == self.transpondersEntry and not self.scan_nims.value == "":
			self["introduction"].setText(_("Left, right or press OK to select transporder."))
		elif not self.scan_nims.value == "":
			self["introduction"].setText(_("Press button OK to start the scan."))
		else:
			if self.scan_nims.value == "":
				self["introduction"].setText(_("Nothing to scan!\nPlease setup your tuner settings before you start a service scan."))

	def updateTuneStatus(self):
		if not self.frontend or not len(self.tpslist):
			return
		stop = False
		dict = {}
		self.frontend.getFrontendStatus(dict)
		state = dict.get("tuner_state")
		if state == "TUNING":
			self.tuneTimer.start(100, True)
		else:
			if state == "LOSTLOCK" or state == "FAILED":
				if len(self.tpslist) > 1:
					self.tpslist_idx += 1
					if self.tpslist_idx >= len(self.tpslist):
						stop = True
						self["status"].setText(_("search failed!"))
						self.tpslist_idx = 0
				else:
					self.relock_fail_count += 1
					if self.relock_fail_count >= self.relock_restart_threshold:
						self.restartFrontendForRelock()
						return
			elif state == "LOCKED":
				if len(self.tpslist) > 1:
					stop = True
				else:
					self.relock_fail_count = 0
					self.tuneTimer.start(250, True)
					return
			else:
				if len(self.tpslist) == 1:
					self.restartFrontendForRelock()
					return
			if not stop:
				self["status"].setText(self.OrbToStr(self.tpslist[self.tpslist_idx][5]) + ": " + str(self.tpslist[self.tpslist_idx][0]) + " " + self.PolToStr(self.tpslist[self.tpslist_idx][2]))
				self.tune(self.tpslist[self.tpslist_idx])
				self.tuneTimer.start(100, True)

	def tune(self, transponder):
		if self.initcomplete:
			if transponder is not None and self.tuner is not None:
				try:
					self.tuner.tune(transponder)
				except Exception as e:
					print(e)

	def retune(self, configElement=None):
		if configElement is None:
			self.tpslist = []
		self.tuneTimer.stop()
		self.relock_fail_count = 0
		if self.scan_nims == []:
			return
		if self.scan_nims.value == "":
			return
		self.tpslist_idx = 0
		tpslist = []
		status_text = ""
		multi_tune = False
		index_to_scan = int(self.scan_nims.value)
		if len(self.satList) <= index_to_scan:
			return
		if len(self.scan_satselection) <= index_to_scan:
			return
		nim = nimmanager.nim_slots[index_to_scan]
		if not nim.isCompatible("DVB-S"):
			return
		nimsats = self.satList[index_to_scan]
		selsatidx = self.scan_satselection[index_to_scan].index
		if self.scan_type.value == "single_transponder":
			if len(nimsats):
				orbpos = nimsats[selsatidx][0]
				if self.scan_sat.system.value == eDVBFrontendParametersSatellite.System_DVB_S:
					fec = self.scan_sat.fec.value
				else:
					fec = self.scan_sat.fec_s2.value
				tpslist.append((self.scan_sat.frequency.value,
						self.scan_sat.symbolrate.value,
						self.scan_sat.polarization.value,
						fec,
						self.scan_sat.inversion.value,
						orbpos,
						self.scan_sat.system.value,
						self.scan_sat.modulation.value,
						self.scan_sat.rolloff.value,
						self.scan_sat.pilot.value,
						self.scan_sat.is_id.value,
						self.scan_sat.pls_mode.value,
						self.scan_sat.pls_code.value,
						self.scan_sat.t2mi_plp_id.value,
						self.scan_sat.t2mi_pid.value))
		elif self.scan_type.value == "predefined_transponder":
			if len(nimsats):
				orbpos = nimsats[selsatidx][0]
				index = self.scan_transponders.index
				if configElement and configElement._value == str(orbpos):
					index = 0
				tps = nimmanager.getTransponders(orbpos)
				if len(tps) > index:
					x = tps[index]
					tpslist.append((x[1] // 1000, x[2] // 1000, x[3], x[4], x[7], orbpos, x[5], x[6], x[8], x[9], x[10], x[11], x[12], x[13], x[14]))
		elif self.scan_type.value == "single_satellite":
			if len(nimsats):
				multi_tune = True
				orbpos = nimsats[selsatidx][0]
				tps = nimmanager.getTransponders(orbpos)
				for x in tps:
					if x[0] == 0:	#SAT
						tpslist.append((x[1] // 1000, x[2] // 1000, x[3], x[4], x[7], orbpos, x[5], x[6], x[8], x[9], x[10], x[11], x[12], x[13], x[14]))
		elif "multisat" in self.scan_type.value:
			if len(self.multiscanlist):
				for sat in self.multiscanlist:
					if sat[1].value or len(tpslist) == 0:
						if len(tpslist):
							del tpslist[:]
						tps = nimmanager.getTransponders(sat[0])
						for x in tps:
							if x[0] == 0:	#SAT
								tpslist.append((x[1] // 1000, x[2] // 1000, x[3], x[4], x[7], sat[0], x[5], x[6], x[8], x[9], x[10], x[11], x[12], x[13], x[14]))
						if sat[1].value:
							multi_tune = True
							break
			else:
				status_text = _("multi scan list empty!")
				SatList = nimmanager.getSatListForNim(index_to_scan)
				for sat in SatList:
					tps = nimmanager.getTransponders(sat[0])
					for x in tps:
						if x[0] == 0:	#SAT
							tpslist.append((x[1] // 1000, x[2] // 1000, x[3], x[4], x[7], sat[0], x[5], x[6], x[8], x[9], x[10], x[11], x[12], x[13], x[14]))
					if len(tpslist):
						break
		elif self.scan_type.value == "provider":
			if self.provider_list is not None:
				if self.provider_list.value != "none":
					orbpos = 0
					providerList = None
					viasat = False
					kontinent = False
					if self.provider_list.value == "viasat":
						orbpos = 49
						providerList = VIASAT
						viasat = True
					elif self.provider_list.value == "viasat_lat":
						orbpos = 49
						providerList = VIASATLATVIJA
						viasat = True
					elif self.provider_list.value == "xtratv":
						orbpos = 90
						providerList = XTRATV
					elif self.provider_list.value == "viasat_ukr":
						orbpos = 3560
						providerList = VIASATUKR
					elif self.provider_list.value == "ntv":
						orbpos = 360
						providerList = NTVPLUS
					elif self.provider_list.value == "tricolor":
						orbpos = 360
						providerList = TRIKOLOR
					elif self.provider_list.value == "ntv_vostok":
						orbpos = 560
						providerList = NTVPLUS_VOSTOK
					elif self.provider_list.value == "tricolor_sibir":
						orbpos = 560
						providerList = TRIKOLOR_SIBIR
					elif self.provider_list.value == "otautv":
						orbpos = 585
						providerList = OTAUTV
					elif self.provider_list.value == "raduga":
						orbpos = 750
						providerList = RADUGA
					elif self.provider_list.value == "mtstv":
						orbpos = 750
						providerList = MTSTV
					elif self.provider_list.value == "kontinent":
						kontinent = True
						orbpos = 850
						providerList = KONTINENT
					elif self.provider_list.value == "ntv_dalvostok":
						orbpos = 1400
						providerList = NTVPLUS_DAL_VOSTOK
					elif self.provider_list.value == "kontinent_dalvostok":
						orbpos = 1400
						providerList = KONTINENT_DAL_VOSTOK
					if orbpos > 0 and providerList is not None:
						SatList = nimmanager.getSatListForNim(index_to_scan)
						for sat in SatList:
							if sat[0] == orbpos or (viasat and sat[0] in (48, 49)) or (kontinent and sat[0] in (849, 850, 851)):
								tps = nimmanager.getTransponders(sat[0])
								for x in tps:
									pol = x[3]
									if x[3] == 2:
										pol = 0
									if x[3] == 3:
										pol = 1
									if (x[1], pol) in providerList:
										tpslist.append((x[1] // 1000, x[2] // 1000, x[3], x[4], x[7], sat[0], x[5], x[6], x[8], x[9], x[10], x[11], x[12], x[13], x[14]))
									if len(tpslist):
										multi_tune = True
				elif self.provider_list.value == "none":
					status_text = _("not selected provider!")
		self.tpslist = tpslist
		if len(self.tpslist):
			status_text = self.OrbToStr(self.tpslist[self.tpslist_idx][5]) + ": " + str(self.tpslist[self.tpslist_idx][0]) + " " + self.PolToStr(self.tpslist[self.tpslist_idx][2])
			self.tune(self.tpslist[self.tpslist_idx])
			self.tuneTimer.start(100, True)
		self["status"].setText(status_text)

	def OrbToStr(self, orbpos=-1):
		if orbpos == -1 or orbpos > 3600:
			return "??"
		if orbpos > 1800:
			return "%d.%dW" % ((3600 - orbpos) // 10, (3600 - orbpos) % 10)
		else:
			return "%d.%dE" % (orbpos // 10, orbpos % 10)

	def PolToStr(self, pol):
		return (pol == 0 and "H") or (pol == 1 and "V") or (pol == 2 and "L") or (pol == 3 and "R") or "??"

	def FecToStr(self, fec):
		return (fec == 0 and "Auto") or (fec == 1 and "1/2") or (fec == 2 and "2/3") or (fec == 3 and "3/4") or \
			(fec == 4 and "5/6") or (fec == 5 and "7/8") or (fec == 6 and "8/9") or (fec == 7 and "3/5") or \
			(fec == 8 and "4/5") or (fec == 9 and "9/10") or (fec == 15 and "None") or "Unknown"

	def updateTranspondersList(self, orbpos, tr=None, pol=None):
		if orbpos is not None:
			index = 0
			default = "0"
			list = []
			tps = nimmanager.getTransponders(orbpos)
			for x in tps:
				if x[0] == 0:	#SAT
					s = str(x[1] // 1000) + " " + self.PolToStr(x[3]) + " / " + str(x[2] // 1000) + " / " + self.FecToStr(x[4])
					list.append((str(index), s))
					if tr is not None and tr == x[1] // 1000 and pol is not None and pol == x[3]:
						default = str(index)
					index += 1
			if orbpos == 360 and len(list) >= 20 and default == "0":
				default = "20"
			self.scan_transponders = ConfigSelection(choices=list, default=default)
			self.scan_transponders.addNotifier(self.retune, initial_call=False)

	def updateSatList(self):
		self.satList = []
		for slot in nimmanager.nim_slots:
			if slot.isCompatible("DVB-S"):
				self.satList.append(nimmanager.getSatListForNim(slot.slot))
			else:
				self.satList.append(None)

	def isT2MI(self):
		if self.feid is not None:
			return os.path.exists("/proc/stb/frontend/%d/t2mi" % self.feid)
		return False

	def createSetup(self, firstStart=None):
		indent = "--"
		self.tuneTimer.stop()
		self.list = []
		self.multiscanlist = []
		if self.scan_nims == [] or self.scan_nims.value == "":
			return
		index_to_scan = int(self.scan_nims.value)
		config_list = True
		self.tunerEntry = getConfigListEntry(_("Tuner"), self.scan_nims)
		self.list.append(self.tunerEntry)
		self.typeOfScanEntry = None
		self.systemEntry = None
		self.satelliteEntry = None
		self.modulationEntry = None
		self.transpondersEntry = None
		self.is_id_boolEntry = None
		self.t2mi_plp_id_boolEntry = None
		self.scan_networkScan.value = False
		nim = nimmanager.nim_slots[index_to_scan]
		if nim.isCompatible("DVB-S"):
			self.typeOfScanEntry = getConfigListEntry(_("Type of scan"), self.scan_type)
			self.list.append(self.typeOfScanEntry)
			if self.scan_type.value == "single_transponder":
				self.updateSatList()
				sat = self.satList[index_to_scan][self.scan_satselection[index_to_scan].index]
				self.updateTranspondersList(sat[0])
				if nim.isCompatible("DVB-S2"):
					self.systemEntry = getConfigListEntry(_('System'), self.scan_sat.system)
					self.list.append(self.systemEntry)
				else:
					self.scan_sat.system.value = eDVBFrontendParametersSatellite.System_DVB_S
				self.list.append(getConfigListEntry(_('Satellite'), self.scan_satselection[index_to_scan]))
				self.list.append(getConfigListEntry(_('Frequency'), self.scan_sat.frequency))
				self.list.append(getConfigListEntry(_('Inversion'), self.scan_sat.inversion))
				self.list.append(getConfigListEntry(_('Symbol Rate'), self.scan_sat.symbolrate))
				self.list.append(getConfigListEntry(_("Polarity"), self.scan_sat.polarization))
				if self.scan_sat.system.value == eDVBFrontendParametersSatellite.System_DVB_S:
					self.list.append(getConfigListEntry(_("FEC"), self.scan_sat.fec))
				elif self.scan_sat.system.value == eDVBFrontendParametersSatellite.System_DVB_S2:
					self.list.append(getConfigListEntry(_("FEC"), self.scan_sat.fec_s2))
					self.modulationEntry = getConfigListEntry(_('Modulation'), self.scan_sat.modulation)
					self.list.append(self.modulationEntry)
					self.list.append(getConfigListEntry(_('Rolloff'), self.scan_sat.rolloff))
					self.list.append(getConfigListEntry(_('Pilot'), self.scan_sat.pilot))
					if hasattr(nim, "isMultistream") and nim.isMultistream():
						self.is_id_boolEntry = getConfigListEntry(_('Transport Stream Type'), self.scan_sat.is_id_bool)
						self.list.append(self.is_id_boolEntry)
						if self.scan_sat.is_id_bool.value:
							self.list.append(getConfigListEntry("%s%s" % (indent, _('Input Stream ID')), self.scan_sat.is_id))
							self.list.append(getConfigListEntry("%s%s" % (indent, _('PLS Mode')), self.scan_sat.pls_mode))
							self.list.append(getConfigListEntry("%s%s" % (indent, _('PLS Code')), self.scan_sat.pls_code))
					else:
						self.scan_sat.is_id.value = eDVBFrontendParametersSatellite.No_Stream_Id_Filter
						self.scan_sat.pls_mode.value = eDVBFrontendParametersSatellite.PLS_Gold
						self.scan_sat.pls_code.value = eDVBFrontendParametersSatellite.PLS_Default_Gold_Code
					if self.isT2MI():
						self.t2mi_plp_id_boolEntry = getConfigListEntry(_('T2MI PLP'), self.scan_sat.t2mi_plp_id_bool)
						self.list.append(self.t2mi_plp_id_boolEntry)
						if self.scan_sat.t2mi_plp_id_bool.value:
							self.list.append(getConfigListEntry("%s%s" % (indent, _('T2MI PLP ID')), self.scan_sat.t2mi_plp_id))
							self.list.append(getConfigListEntry("%s%s" % (indent, _('T2MI PID')), self.scan_sat.t2mi_pid))
					else:
						self.scan_sat.t2mi_plp_id.value = eDVBFrontendParametersSatellite.No_T2MI_PLP_Id
						self.scan_sat.t2mi_pid.value = eDVBFrontendParametersSatellite.T2MI_Default_Pid
			elif self.scan_type.value == "predefined_transponder":
				self.updateSatList()
				self.satelliteEntry = getConfigListEntry(_('Satellite'), self.scan_satselection[index_to_scan])
				self.list.append(self.satelliteEntry)
				sat = self.satList[index_to_scan][self.scan_satselection[index_to_scan].index]
				if firstStart is not None:
					self.updateTranspondersList(sat[0], tr=self.scan_sat.frequency.value, pol=self.scan_sat.polarization.value)
				else:
					self.updateTranspondersList(sat[0])
				self.transpondersEntry = getConfigListEntry(_('Transponder'), self.scan_transponders)
				self.list.append(self.transpondersEntry)
			elif self.scan_type.value == "single_satellite":
				self.updateSatList()
				sat = self.satList[index_to_scan][self.scan_satselection[index_to_scan].index]
				self.updateTranspondersList(sat[0])
				self.list.append(getConfigListEntry(_("Satellite"), self.scan_satselection[index_to_scan]))
				self.scan_networkScan.value = True
			elif "multisat" in self.scan_type.value:
				tlist = []
				SatList = nimmanager.getSatListForNim(index_to_scan)
				for x in SatList:
					if self.Satexists(tlist, x[0]) == 0:
						tlist.append(x[0])
						sat = ConfigEnableDisable(default=self.scan_type.value.find("_yes") != -1 and True or False)
						configEntry = getConfigListEntry(nimmanager.getSatDescription(x[0]), sat)
						self.list.append(configEntry)
						self.multiscanlist.append((x[0], sat))
						sat.addNotifier(self.retune, initial_call=False)
				self.scan_networkScan.value = True
			elif self.scan_type.value == "provider":
				config_list = False
				satList = nimmanager.getSatListForNim(index_to_scan)
				satchoises = [("none", _("None"))]
				for sat in satList:
					if sat[0] == 48 or sat[0] == 49:
						satchoises.append(("viasat", _("Viasat")))
						satchoises.append(("viasat_lat", _("Viasat Latvija")))
					elif sat[0] == 3560:
						satchoises.append(("viasat_ukr", _("Viasat Ukraine") + " (Amos)"))
					elif sat[0] == 90:
						satchoises.append(("xtratv", _("Xtra TV")))
					elif sat[0] == 360:
						satchoises.append(("ntv", _("NTV Plus")))
						satchoises.append(("tricolor", _("Tricolor TV")))
					elif sat[0] == 560:
						satchoises.append(("ntv_vostok", _("NTV Plus Vostok")))
						satchoises.append(("tricolor_sibir", _("Tricolor TV Sibir")))
					elif sat[0] == 585:
						satchoises.append(("otautv", _("Otau TV")))
					elif sat[0] == 750:
						#satchoises.append(("raduga", _("Raduga TV")))
						satchoises.append(("mtstv", _("MTS TV")))
					elif sat[0] in (849, 850, 851):
						satchoises.append(("kontinent", _("Telekarta (HD)")))
					elif sat[0] == 1400:
						satchoises.append(("ntv_dalvostok", _("NTV Plus Dalniy Vostok")))
						satchoises.append(("kontinent_dalvostok", _("Telekarta-Vostok")))
				self.provider_list = ConfigSelection(default="none", choices=satchoises)
				ProviderEntry = getConfigListEntry(_("Provider"), self.provider_list)
				self.list.append(ProviderEntry)
				self.provider_list.addNotifier(self.retune, initial_call=False)
				if self.provider_list.value == "none":
					self.retune(None)
		self.list.append(getConfigListEntry(_("Network scan"), self.scan_networkScan))
		clear_text = _("Clear before scan")
		if self.scan_type.value == "predefined_transponder" or self.scan_type.value == "single_transponder":
			clear_text += _(" (only this transponder)")
		elif self.scan_type.value == "provider":
			clear_text += _(" (only transponders provider)")
		self.list.append(getConfigListEntry(clear_text, self.scan_clearallservices))
		if config_list:
			self.list.append(getConfigListEntry(_("Only free scan"), self.scan_onlyfree))
		self["config"].list = self.list
		self["config"].l.setList(self.list)

	def Satexists(self, tlist, pos):
		for x in tlist:
			if x == pos:
				return 1
		return 0

	def newConfig(self):
		cur = self["config"].getCurrent()
		if cur is None:
			return
		if cur == self.tunerEntry:
			if not self.scan_nims.value == "":
				self.feid = int(self.scan_nims.value)
				self.deInitFrontend()
				self.initFrontend()
				self.createSetup()
		elif cur == self.typeOfScanEntry or \
			cur == self.systemEntry or \
			cur == self.satelliteEntry or \
			(self.modulationEntry and self.systemEntry[1].value == eDVBFrontendParametersSatellite.System_DVB_S2 and cur == self.modulationEntry):
			self.createSetup()
		elif cur == self.is_id_boolEntry:
			if self.is_id_boolEntry[1].value:
				self.scan_sat.is_id.value = 0 if self.is_id_memory < 0 else self.is_id_memory
				self.scan_sat.pls_mode.value = self.pls_mode_memory
				self.scan_sat.pls_code.value = self.pls_code_memory
			else:
				self.is_id_memory = self.scan_sat.is_id.value
				self.pls_mode_memory = self.scan_sat.pls_mode.value
				self.pls_code_memory = self.scan_sat.pls_code.value
				self.scan_sat.is_id.value = eDVBFrontendParametersSatellite.No_Stream_Id_Filter
				self.scan_sat.pls_mode.value = eDVBFrontendParametersSatellite.PLS_Gold
				self.scan_sat.pls_code.value = eDVBFrontendParametersSatellite.PLS_Default_Gold_Code
			self.createSetup()
			self.retune()
		elif cur == self.t2mi_plp_id_boolEntry:
			if self.t2mi_plp_id_boolEntry[1].value:
				self.scan_sat.t2mi_plp_id.value = 0 if self.t2mi_plp_id_memory < 0 else self.t2mi_plp_id_memory
				self.scan_sat.t2mi_pid.value = self.t2mi_pid_memory
			else:
				self.t2mi_plp_id_memory = self.scan_sat.t2mi_plp_id.value
				self.t2mi_pid_memory = self.scan_sat.t2mi_pid.value
				self.scan_sat.t2mi_plp_id.value = eDVBFrontendParametersSatellite.No_T2MI_PLP_Id
				self.scan_sat.t2mi_pid.value = eDVBFrontendParametersSatellite.T2MI_Default_Pid
			self.createSetup()
			self.retune()

	def getNimvalue(self):
		nim_list = []
		for n in nimmanager.nim_slots:
			if hasattr(n, 'config_mode'):
				if n.config_mode == "nothing":
					continue
				if hasattr(n, "isFBCLink") and n.isFBCLink():
					continue
				if n.config_mode in ("simple", "equal", "advanced") and len(nimmanager.getSatListForNim(n.slot)) < 1:
					continue
				if n.config_mode in ("loopthrough", "satposdepends"):
					root_id = nimmanager.sec.getRoot(n.slot_id, int(n.config.connectedTo.value))
					if n.type == nimmanager.nim_slots[root_id].type:
						continue
				if n.isCompatible("DVB-S"):
					nim_list.append((str(n.slot), n.friendly_full_description))
			elif hasattr(n, 'config') and hasattr(n.config, 'dvbs') and hasattr(n.config.dvbs, 'configMode'):
				if n.config.dvbs.configMode.value == "nothing":
					continue
				if hasattr(n, "isFBCLink") and n.isFBCLink():
					continue
				if n.config.dvbs.configMode.value in ("simple", "equal", "advanced") and len(nimmanager.getSatListForNim(n.slot)) < 1:
					continue
				if n.config.dvbs.configMode.value in ("loopthrough", "satposdepends"):
					root_id = nimmanager.sec.getRoot(n.slot_id, int(n.config.dvbs.connectedTo.value))
					if n.type == nimmanager.nim_slots[root_id].type:
						continue
				if n.isCompatible("DVB-S"):
					nim_list.append((str(n.slot), n.friendly_full_description))
		self.scan_nims = ConfigSelection(choices=nim_list)
		return nim_list

	def createConfig(self, frontendData):
		defaultSat = {
			"orbpos": 192,
			"system": eDVBFrontendParametersSatellite.System_DVB_S,
			"frequency": 11836,
			"inversion": eDVBFrontendParametersSatellite.Inversion_Unknown,
			"symbolrate": 27500,
			"polarization": eDVBFrontendParametersSatellite.Polarisation_Horizontal,
			"fec": eDVBFrontendParametersSatellite.FEC_Auto,
			"fec_s2": eDVBFrontendParametersSatellite.FEC_9_10,
			"modulation": eDVBFrontendParametersSatellite.Modulation_QPSK,
			"is_id": 0,
			"pls_mode": eDVBFrontendParametersSatellite.PLS_Gold,
			"pls_code": 0,
			"t2mi_plp_id": eDVBFrontendParametersSatellite.No_T2MI_PLP_Id,
			"t2mi_pid": eDVBFrontendParametersSatellite.T2MI_Default_Pid}

		default_scan = "single_transponder"
		if frontendData is not None:
			ttype = frontendData.get("tuner_type", "UNKNOWN")
			if ttype == "DVB-S":
				defaultSat["system"] = frontendData.get("system", eDVBFrontendParametersSatellite.System_DVB_S)
				defaultSat["frequency"] = frontendData.get("frequency", 0) // 1000
				defaultSat["inversion"] = frontendData.get("inversion", eDVBFrontendParametersSatellite.Inversion_Unknown)
				defaultSat["symbolrate"] = frontendData.get("symbol_rate", 0) // 1000
				defaultSat["polarization"] = frontendData.get("polarization", eDVBFrontendParametersSatellite.Polarisation_Horizontal)
				if defaultSat["system"] == eDVBFrontendParametersSatellite.System_DVB_S2:
					defaultSat["fec_s2"] = frontendData.get("fec_inner", eDVBFrontendParametersSatellite.FEC_Auto)
					defaultSat["rolloff"] = frontendData.get("rolloff", eDVBFrontendParametersSatellite.RollOff_alpha_0_35)
					defaultSat["pilot"] = frontendData.get("pilot", eDVBFrontendParametersSatellite.Pilot_Unknown)
				else:
					defaultSat["fec"] = frontendData.get("fec_inner", eDVBFrontendParametersSatellite.FEC_Auto)
				defaultSat["modulation"] = frontendData.get("modulation", eDVBFrontendParametersSatellite.Modulation_QPSK)
				if 'orbital_position' in frontendData:
					defaultSat["orbpos"] = frontendData['orbital_position']
				defaultSat["is_id"] = frontendData.get("is_id", 0)
				defaultSat["pls_mode"] = frontendData.get("pls_mode", eDVBFrontendParametersSatellite.PLS_Gold)
				defaultSat["pls_code"] = frontendData.get("pls_code", 0)
				defaultSat["t2mi_plp_id"] = frontendData.get("t2mi_plp_id", eDVBFrontendParametersSatellite.No_T2MI_PLP_Id)
				defaultSat["t2mi_pid"] = frontendData.get("t2mi_pid", eDVBFrontendParametersSatellite.T2MI_Default_Pid)
				default_scan = "predefined_transponder"

		self.scan_sat = ConfigSubsection()
		scan_choices = {
		"single_transponder": _("User defined transponder"),
		"predefined_transponder": _("Predefined transponder"),
		"single_satellite": _("Single satellite"),
		"multisat": _("Multisat"),
		"multisat_yes": _("Multisat all select")}
		if self.providersSat():
			scan_choices = {
			"single_transponder": _("User defined transponder"),
			"predefined_transponder": _("Predefined transponder"),
			"single_satellite": _("Single satellite"),
			"multisat": _("Multisat"),
			"multisat_yes": _("Multisat all select"),
			"provider": _("Russian providers")}
		self.scan_type = ConfigSelection(choices=scan_choices, default=default_scan)
		self.scan_transponders = None
		self.provider_list = None
		self.scan_clearallservices = ConfigSelection(default="no", choices=[("no", _("no")), ("yes", _("yes")), ("yes_hold_feeds", _("yes (keep feeds)"))])
		self.scan_onlyfree = ConfigYesNo(default=False)
		self.scan_networkScan = ConfigYesNo(default=False)

		self.scan_sat.system = ConfigSelection(default=defaultSat["system"], choices=[
			(eDVBFrontendParametersSatellite.System_DVB_S, _("DVB-S")),
			(eDVBFrontendParametersSatellite.System_DVB_S2, _("DVB-S2"))])
		self.scan_sat.frequency = ConfigInteger(default=defaultSat["frequency"], limits=(1, 99999))
		self.scan_sat.inversion = ConfigSelection(default=defaultSat["inversion"], choices=[
			(eDVBFrontendParametersSatellite.Inversion_Off, _("Off")),
			(eDVBFrontendParametersSatellite.Inversion_On, _("On")),
			(eDVBFrontendParametersSatellite.Inversion_Unknown, _("Auto"))])
		self.scan_sat.symbolrate = ConfigInteger(default=defaultSat["symbolrate"], limits=(1, 99999))
		self.scan_sat.polarization = ConfigSelection(default=defaultSat["polarization"], choices=[
			(eDVBFrontendParametersSatellite.Polarisation_Horizontal, _("horizontal")),
			(eDVBFrontendParametersSatellite.Polarisation_Vertical, _("vertical")),
			(eDVBFrontendParametersSatellite.Polarisation_CircularLeft, _("circular left")),
			(eDVBFrontendParametersSatellite.Polarisation_CircularRight, _("circular right"))])
		self.scan_sat.fec = ConfigSelection(default=defaultSat["fec"], choices=[
			(eDVBFrontendParametersSatellite.FEC_Auto, _("Auto")),
			(eDVBFrontendParametersSatellite.FEC_1_2, "1/2"),
			(eDVBFrontendParametersSatellite.FEC_2_3, "2/3"),
			(eDVBFrontendParametersSatellite.FEC_3_4, "3/4"),
			(eDVBFrontendParametersSatellite.FEC_5_6, "5/6"),
			(eDVBFrontendParametersSatellite.FEC_7_8, "7/8"),
			(eDVBFrontendParametersSatellite.FEC_None, _("None"))])
		self.scan_sat.fec_s2 = ConfigSelection(default=defaultSat["fec_s2"], choices=[
			(eDVBFrontendParametersSatellite.FEC_Auto, _("Auto")),
			(eDVBFrontendParametersSatellite.FEC_1_2, "1/2"),
			(eDVBFrontendParametersSatellite.FEC_2_3, "2/3"),
			(eDVBFrontendParametersSatellite.FEC_3_4, "3/4"),
			(eDVBFrontendParametersSatellite.FEC_3_5, "3/5"),
			(eDVBFrontendParametersSatellite.FEC_4_5, "4/5"),
			(eDVBFrontendParametersSatellite.FEC_5_6, "5/6"),
			(eDVBFrontendParametersSatellite.FEC_7_8, "7/8"),
			(eDVBFrontendParametersSatellite.FEC_8_9, "8/9"),
			(eDVBFrontendParametersSatellite.FEC_9_10, "9/10")])
		lst = [(eDVBFrontendParametersSatellite.Modulation_QPSK, "QPSK"), (eDVBFrontendParametersSatellite.Modulation_8PSK, "8PSK")]
		if hasattr(eDVBFrontendParametersSatellite, "Modulation_16APSK") and hasattr(eDVBFrontendParametersSatellite, "Modulation_32APSK"):
			lst += [(eDVBFrontendParametersSatellite.Modulation_16APSK, "16APSK"), (eDVBFrontendParametersSatellite.Modulation_32APSK, "32APSK")]
		self.scan_sat.modulation = ConfigSelection(default=defaultSat["modulation"], choices=lst)
		lst = [(eDVBFrontendParametersSatellite.RollOff_alpha_0_35, "0.35"), (eDVBFrontendParametersSatellite.RollOff_alpha_0_25, "0.25"), (eDVBFrontendParametersSatellite.RollOff_alpha_0_20, "0.20")]
		if hasattr(eDVBFrontendParametersSatellite, "RollOff_auto"):
			lst += [(eDVBFrontendParametersSatellite.RollOff_auto, _("Auto"))]
		self.scan_sat.rolloff = ConfigSelection(default=defaultSat.get("rolloff", eDVBFrontendParametersSatellite.RollOff_alpha_0_35), choices=lst)
		self.scan_sat.pilot = ConfigSelection(default=defaultSat.get("pilot", eDVBFrontendParametersSatellite.Pilot_Unknown), choices=[
			(eDVBFrontendParametersSatellite.Pilot_Off, _("Off")),
			(eDVBFrontendParametersSatellite.Pilot_On, _("On")),
			(eDVBFrontendParametersSatellite.Pilot_Unknown, _("Auto"))])
		self.scan_sat.is_id_bool = ConfigSelection(default=defaultSat["is_id"] != eDVBFrontendParametersSatellite.No_Stream_Id_Filter, choices=[(True, _("Multistream")), (False, _("Ordinary"))])
		self.scan_sat.is_id = ConfigInteger(default=defaultSat["is_id"], limits=(0, 255))
		self.scan_sat.pls_mode = ConfigSelection(default=defaultSat["pls_mode"], choices=[
			(eDVBFrontendParametersSatellite.PLS_Root, _("Root")),
			(eDVBFrontendParametersSatellite.PLS_Gold, _("Gold")),
			(eDVBFrontendParametersSatellite.PLS_Combo, _("Combo"))])
		self.scan_sat.pls_code = ConfigInteger(default=defaultSat.get("pls_code", 0), limits=(0, 262142))
		self.scan_sat.t2mi_plp_id = ConfigInteger(default=defaultSat.get("t2mi_plp_id", eDVBFrontendParametersSatellite.No_T2MI_PLP_Id), limits=(0, 255))
		self.scan_sat.t2mi_pid = ConfigInteger(default=defaultSat.get("t2mi_pid", eDVBFrontendParametersSatellite.T2MI_Default_Pid), limits=(0, 8191))
		self.scan_sat.t2mi_plp_id_bool = ConfigSelection(default=defaultSat["t2mi_plp_id"] != eDVBFrontendParametersSatellite.No_T2MI_PLP_Id, choices=[(True, _("Enabled")), (False, _("Disabled"))])

		self.is_id_memory = self.scan_sat.is_id.value # used to prevent is_id value being lost when self.scan_sat.is_id_bool state changes
		self.pls_mode_memory = self.scan_sat.pls_mode.value
		self.pls_code_memory = self.scan_sat.pls_code.value
		self.t2mi_plp_id_memory = self.scan_sat.t2mi_plp_id.value
		self.t2mi_pid_memory = self.scan_sat.t2mi_pid.value

		self.scan_scansat = {}
		for sat in nimmanager.satList:
			self.scan_scansat[sat[0]] = ConfigYesNo(default=False)

		self.scan_satselection = []
		for slot in nimmanager.nim_slots:
			if slot.isCompatible("DVB-S"):
				x = getConfigSatlist(defaultSat["orbpos"], self.satList[slot.slot])
				x.addNotifier(self.retune, initial_call=False)
				self.scan_satselection.append(x)
			else:
				self.scan_satselection.append(None)

		for x in (self.scan_nims, self.scan_type, self.scan_sat.frequency,
			self.scan_sat.inversion, self.scan_sat.symbolrate,
			self.scan_sat.polarization, self.scan_sat.fec, self.scan_sat.pilot,
			self.scan_sat.fec_s2, self.scan_sat.fec, self.scan_sat.modulation,
			self.scan_sat.is_id, self.scan_sat.pls_mode, self.scan_sat.pls_code,
			self.scan_sat.rolloff, self.scan_sat.system, self.scan_sat.t2mi_plp_id, self.scan_sat.t2mi_pid):
			x.addNotifier(self.retune, initial_call=False)

	def keyLeft(self):
		ConfigListScreen.keyLeft(self)
		self.newConfig()

	def keyRight(self):
		ConfigListScreen.keyRight(self)
		self.newConfig()

	def addSatTransponder(self, tlist, frequency, symbol_rate, polarisation, fec, inversion, orbital_position, system, modulation, rolloff, pilot, is_id, pls_mode, pls_code, t2mi_plp_id, t2mi_pid):
		#print("Add Sat: frequency: " + str(frequency) + " symbol: " + str(symbol_rate) + " pol: " + str(polarisation) + " fec: " + str(fec) + " inversion: " + str(inversion) + " modulation: " + str(modulation) + " system: " + str(system) + " rolloff" + str(rolloff) + " pilot" + str(pilot) + " is_id" + str(is_id) + " pls_mode" + str(pls_mode) + " pls_code" + str(pls_code))
		parm = eDVBFrontendParametersSatellite()
		parm.modulation = modulation
		parm.system = system
		parm.frequency = frequency * 1000
		parm.symbol_rate = symbol_rate * 1000
		parm.polarisation = polarisation
		parm.fec = fec
		parm.inversion = inversion
		parm.orbital_position = orbital_position
		parm.rolloff = rolloff
		parm.pilot = pilot
		parm.is_id = is_id
		parm.pls_mode = pls_mode
		parm.pls_code = pls_code
		parm.t2mi_plp_id = t2mi_plp_id
		parm.t2mi_pid = t2mi_pid
		tlist.append(parm)

	def getInitialTransponderList(self, tlist, pos):
		list = nimmanager.getTransponders(pos)
		for x in list:
			if x[0] == 0:
				parm = eDVBFrontendParametersSatellite()
				parm.frequency = x[1]
				parm.symbol_rate = x[2]
				parm.polarisation = x[3]
				parm.fec = x[4]
				parm.inversion = x[7]
				parm.orbital_position = pos
				parm.system = x[5]
				parm.modulation = x[6]
				parm.rolloff = x[8]
				parm.pilot = x[9]
				parm.is_id = x[10]
				parm.pls_mode = x[11]
				parm.pls_code = x[12]
				parm.t2mi_plp_id = x[13]
				parm.t2mi_pid = x[14]
				tlist.append(parm)

	def getInitialTransponderProviderList(self, tlist, pos, providers=None):
		list = nimmanager.getTransponders(pos)
		for x in list:
			pol = x[3]
			if x[3] == 2:
				pol = 0
			if x[3] == 3:
				pol = 1
			if x[0] == 0 and providers is not None and (x[1], pol) in providers:
				fec = x[4]
				system = x[5]
				if providers is TRIKOLOR and x[1] == 12360000 and (x[3] == 3 or x[3] == 1):
					fec = 0
					system = 1
				elif providers is KONTINENT and x[1] == 11960000 and x[3] == 0:
					fec = 2
				parm = eDVBFrontendParametersSatellite()
				parm.frequency = x[1]
				parm.symbol_rate = x[2]
				parm.polarisation = x[3]
				parm.fec = fec
				parm.inversion = x[7]
				parm.orbital_position = pos
				parm.system = system
				parm.modulation = x[6]
				parm.rolloff = x[8]
				parm.pilot = x[9]
				parm.is_id = x[10]
				parm.pls_mode = x[11]
				parm.pls_code = x[12]
				parm.t2mi_plp_id = x[13]
				parm.t2mi_pid = x[14]
				tlist.append(parm)

	def providersSat(self):
		providers_sat = False
		for sat in nimmanager.satList:
			if sat[0] == 48 or sat[0] == 49 or sat[0] == 90 or sat[0] == 360 or sat[0] == 560 or sat[0] == 585 or sat[0] == 750 or sat[0] in (849, 850, 851) or sat[0] == 1400 or sat[0] == 3560:
				providers_sat = True
				break
		return providers_sat

	def keyOK(self):
		try:
			cur = self["config"].getCurrent()
			if self.scan_type.value == "predefined_transponder" and self.transpondersEntry is not None and cur == self.transpondersEntry:
				index_to_scan = int(self.scan_nims.value)
				sat = self.satList[index_to_scan][self.scan_satselection[index_to_scan].index]
				if sat[0] is not None:
					tr_list = self.createTranspondersList(sat[0])
					if len(tr_list) > 1:
						orbos = self.OrbToStr(sat[0])
						self.session.openWithCallback(self.configCallback, TranspondersList, list=tr_list, sat=orbos)
			else:
				self.keyGo()
		except:
			pass

	def configCallback(self, callback=None):
		try:
			if callback is not None:
				self.scan_transponders.value = callback
		except:
			pass

	def createTranspondersList(self, orbpos):
		list = []
		index = 0
		tps = nimmanager.getTransponders(orbpos)
		for x in tps:
			if x[0] == 0:
				s = str(x[1] // 1000) + " " + self.PolToStr(x[3]) + " / " + str(x[2] // 1000) + " / " + self.FecToStr(x[4])
				list.append((s, str(index)))
				index += 1
		return list

	def keyGo(self):
		if self.frontend is None:
			text = _("Sorry, this tuner is in use.")
			if self.session.nav.getRecordings():
				text += "\n"
				text += _("Maybe the reason that recording is currently running.")
			self.session.open(MessageBox, text, MessageBox.TYPE_ERROR)
			return
		if self.scan_nims.value == "":
			return
		self.tuneTimer.stop()
		self.deInitFrontend()
		index_to_scan = int(self.scan_nims.value)
		self.feid = index_to_scan
		tlist = []
		flags = None
		startScan = True
		removeAll = True
		if self.scan_nims == []:
			self.session.open(MessageBox, _("No tuner is enabled!\nPlease setup your tuner settings before you start a service scan."), MessageBox.TYPE_ERROR)
			return
		nim = nimmanager.nim_slots[index_to_scan]
		if not nim.isCompatible("DVB-S"):
			return
		if self.scan_type.value.find("_transponder") != -1:
			assert len(self.satList) > index_to_scan
			assert len(self.scan_satselection) > index_to_scan
			nimsats = self.satList[index_to_scan]
			selsatidx = self.scan_satselection[index_to_scan].index
			if len(nimsats):
				orbpos = nimsats[selsatidx][0]
				if self.scan_type.value == "single_transponder":
					if self.scan_sat.system.value == eDVBFrontendParametersSatellite.System_DVB_S:
						fec = self.scan_sat.fec.value
					else:
						fec = self.scan_sat.fec_s2.value
					self.addSatTransponder(tlist, self.scan_sat.frequency.value,
								self.scan_sat.symbolrate.value,
								self.scan_sat.polarization.value,
								fec,
								self.scan_sat.inversion.value,
								orbpos,
								self.scan_sat.system.value,
								self.scan_sat.modulation.value,
								self.scan_sat.rolloff.value,
								self.scan_sat.pilot.value,
								self.scan_sat.is_id.value,
								self.scan_sat.pls_mode.value,
								self.scan_sat.pls_code.value,
								self.scan_sat.t2mi_plp_id.value,
								self.scan_sat.t2mi_pid.value)
				elif self.scan_type.value == "predefined_transponder":
					tps = nimmanager.getTransponders(orbpos)
					if len(tps) > self.scan_transponders.index:
						x = tps[self.scan_transponders.index]
						self.addSatTransponder(tlist, x[1] // 1000, x[2] // 1000, x[3], x[4], x[7], orbpos, x[5], x[6], x[8], x[9], x[10], x[11], x[12], x[13], x[14])
			removeAll = False
		elif self.scan_type.value == "single_satellite":
			sat = self.satList[index_to_scan][self.scan_satselection[index_to_scan].index]
			self.getInitialTransponderList(tlist, sat[0])
		elif self.scan_type.value.find("multisat") != -1:
			SatList = nimmanager.getSatListForNim(index_to_scan)
			for x in self.multiscanlist:
				if x[1].value:
					self.getInitialTransponderList(tlist, x[0])
		elif self.scan_type.value == "provider":
			if self.provider_list is not None:
				if self.provider_list.value == "none":
					startScan = False
				else:
					orbpos = 0
					startScan = False
					providerList = None
					viasat = False
					kontinent = False
					if self.provider_list.value == "viasat":
						orbpos = 49
						providerList = VIASAT
						viasat = True
					elif self.provider_list.value == "viasat_lat":
						orbpos = 49
						providerList = VIASATLATVIJA
						viasat = True
					elif self.provider_list.value == "xtratv":
						orbpos = 90
						providerList = XTRATV
					elif self.provider_list.value == "viasat_ukr":
						orbpos = 3560
						providerList = VIASATUKR
					elif self.provider_list.value == "ntv":
						orbpos = 360
						providerList = NTVPLUS
					elif self.provider_list.value == "tricolor":
						orbpos = 360
						providerList = TRIKOLOR
					elif self.provider_list.value == "ntv_vostok":
						orbpos = 560
						providerList = NTVPLUS_VOSTOK
					elif self.provider_list.value == "tricolor_sibir":
						orbpos = 560
						providerList = TRIKOLOR_SIBIR
					elif self.provider_list.value == "otautv":
						orbpos = 585
						providerList = OTAUTV
					elif self.provider_list.value == "raduga":
						orbpos = 750
						providerList = RADUGA
					elif self.provider_list.value == "mtstv":
						orbpos = 750
						providerList = MTSTV
					elif self.provider_list.value == "kontinent":
						orbpos = 850
						providerList = KONTINENT
						kontinent = True
					elif self.provider_list.value == "ntv_dalvostok":
						orbpos = 1400
						providerList = NTVPLUS_DAL_VOSTOK
					elif self.provider_list.value == "kontinent_dalvostok":
						orbpos = 1400
						providerList = KONTINENT_DAL_VOSTOK
					if orbpos > 0 and providerList is not None:
						SatList = nimmanager.getSatListForNim(index_to_scan)
						for sat in SatList:
							if sat[0] == orbpos or (viasat and sat[0] in (48, 49)) or (kontinent and sat[0] in (849, 850, 851)):
								self.getInitialTransponderProviderList(tlist, sat[0], providers=providerList)
						removeAll = False
						startScan = True

		flags = self.scan_networkScan.value and eComponentScan.scanNetworkSearch or 0
		tmp = self.scan_clearallservices.value
		self.kept_feed_snapshot = None
		if tmp == "yes":
			flags |= eComponentScan.scanRemoveServices
		elif tmp == "yes_hold_feeds":
			current_orb = None
			try:
				# Check which configuration variable holds the satellite position
				if hasattr(self, 'scan_sat') and hasattr(self.scan_sat, 'value'):
					current_orb = int(self.scan_sat.value)
				elif hasattr(self, 'satList') and hasattr(self.satList, 'value'):
					current_orb = int(self.satList.value)
			except:
				pass

			# Pass the current satellite position to only snapshot feeds on THIS sat
			self.kept_feed_snapshot = collectKeepServicesSnapshot(orb_filter=current_orb)
			flags |= eComponentScan.scanRemoveServices

		if tmp == "yes" and not removeAll:
			flags |= eComponentScan.scanDontRemoveUnscanned

		if self.scan_onlyfree.value:
			flags |= eComponentScan.scanOnlyFree

		for x in self["config"].list:
			x[1].save()

		if startScan:
			self.startScan(tlist, flags, index_to_scan)

	def keyCancel(self):
		if self.session.postScanService and self.stop_service:
			self.session.openWithCallback(self.restartPrevService, MessageBox, _("Zap back to service before a service scan?"), MessageBox.TYPE_YESNO, timeout=10)
		else:
			self.restartPrevService(True)

	def restartPrevService(self, answer):
		if self.scan_clearallservices.value == "yes_hold_feeds":
			appendSnapshotServicesToLamedb(getattr(self, "kept_feed_snapshot", None))
			self.kept_feed_snapshot = None
		for x in self["config"].list:
			x[1].cancel()
		self.tuneTimer.stop()
		self.deInitFrontend()
		if answer:
			self.session.nav.playService(self.session.postScanService)
		self.close()

	def handleKeyFileCallback(self, answer):
		ConfigListScreen.handleKeyFileCallback(self, answer)
		self.newConfig()

	def startScan(self, tlist, flags, feid):
		if len(tlist):
			self.session.openWithCallback(self.serviceScanFinished, ServiceScan, [{"transponders": tlist, "feid": feid, "flags": flags}])
		else:
			self.session.open(MessageBox, _("Nothing to scan!\nPlease setup your tuner settings before you start a service scan."), MessageBox.TYPE_ERROR)
			self.keyCancel()

	def serviceScanFinished(self, answer=None):
		if answer is not True:
			self.session.openWithCallback(self.restartSignalFinder, MessageBox, _("Do you want to scan another transponder/satellite?"), MessageBox.TYPE_YESNO, timeout=10)
		elif answer is True:
			self.restartPrevService(True)

	def restartSignalFinder(self, answer):
		if answer:
			self.tuneTimer.stop()
			self.deInitFrontend()
			self.initFrontend()
		else:
			self.keyCancel()


class SignalFinderMultistream(ConfigListScreen, Screen):
	if isHD():
		skin = """
			<screen position="center,center" size="1200,635" title="Signal finder" >
				<widget name="pos" position="10,10" size="210,25" font="Regular;22" halign="right" transparent="1" />
				<widget name="status" position="230,10" size="370,25" font="Regular;22" halign="left" foregroundColor="#f8f711" transparent="1" />
				<widget source="Frontend" render="Progress" pixmap="/usr/lib/enigma2/python/Plugins/SystemPlugins/Signalfinder/image/bar_big.png" position="140,40" size="1000,50" borderColor="#00808888">
					<convert type="FrontendInfo">SNR</convert>
				</widget>
				<eLabel text="SNR:" position="145,50" size="100,35" backgroundColor="#00666666" transparent="1" font="Regular;35" />
				<widget source="Frontend" render="Label" position="910,50" size="225,35" halign="right" transparent="1" font="Regular;35">
					<convert type="FrontendInfo">SNR</convert>
				</widget>
				<widget source="Frontend" render="Progress" pixmap="/usr/lib/enigma2/python/Plugins/SystemPlugins/Signalfinder/image/bar_agc.png" position="140,100" size="1000,50" borderColor="#00808888">
					<convert type="FrontendInfo">AGC</convert>
				</widget>
				<eLabel text="AGC:" position="145,110" size="100,35" backgroundColor="#00666666" transparent="1" font="Regular;35" />
				<widget source="Frontend" render="Label" position="910,110" size="225,35" halign="right" transparent="1" font="Regular;35">
					<convert type="FrontendInfo">AGC</convert>
				</widget>
				<widget source="Frontend" render="Progress" pixmap="/usr/lib/enigma2/python/Plugins/SystemPlugins/Signalfinder/image/bar_ber.png" position="140,160" size="1000,50" borderColor="#00808888">
					<convert type="FrontendInfo">BER</convert>
				</widget>
				<eLabel text="BER:" position="145,170" size="100,35" backgroundColor="#00666666"  transparent="1" font="Regular;35" />
				<widget source="Frontend" render="Label" position="910,170" size="225,35" halign="right" transparent="1" font="Regular;35">
					<convert type="FrontendInfo">BER</convert>
				</widget>
				<eLabel text="SNR:" position="140,225" size="120,20" backgroundColor="#00666666" transparent="1" zPosition="5" font="Regular;18" />
				<widget source="Frontend" render="Label" position="140,245" size="310,75" font="Regular;72" halign="left" backgroundColor="#00666666" transparent="1">
					<convert type="FrontendInfo">SNRdB</convert>
				</widget>
				<eLabel text="AGC:" position="140,325" size="120,20" backgroundColor="#00666666" transparent="1" zPosition="5" font="Regular;18" />
				<widget source="Frontend" render="Label" position="140,345" size="310,75" backgroundColor="#00666666" transparent="1" font="Regular;72" halign="left">
					<convert type="FrontendInfo">AGC</convert>
				</widget>
				<eLabel text="BER:" position="140,425" size="120,20" backgroundColor="#00666666" transparent="1" zPosition="5" font="Regular;18" />
				<widget source="Frontend" render="Label" position="140,445" size="310,75" font="Regular;72" halign="left" backgroundColor="#00666666" transparent="1">
					<convert type="FrontendInfo">BER</convert>
				</widget>
				<widget text="LOCK" source="Frontend" render="FixedLabel" position="140,525" size="310,90" font="Regular;72" halign="left" foregroundColor="#00389416" backgroundColor="#00666666" transparent="1" >
					<convert type="FrontendInfo">LOCK</convert>
					<convert type="ConditionalShowHide" />
				</widget>
				<widget name="config" position="420,225" size="720,396" transparent="1" scrollbarMode="showOnDemand" />
				<widget name="introduction" position="540,10" size="600,30"  zPosition="1" transparent="1" foregroundColor="#f8f711" font="Regular;22" />
				<widget name="Cancel" position="200,610" size="250,28" foregroundColor="#00ff2525" zPosition="1" transparent="1" font="Regular;24" />
				<widget name="Scan" position="750,610" size="250,28" foregroundColor="#00389416" zPosition="1" transparent="1" font="Regular;24" />
			</screen>"""
	else:
		skin = """
  <screen position="center,center" size="1020,900" title="Signal finder">
    <widget name="pos" position="20,5" size="300,33" font="Regular; 26" halign="right" transparent="1" />
    <widget name="status" position="325,5" size="650,33" font="Regular; 26" halign="left" foregroundColor="#f8f711" transparent="1" />
    <widget source="Frontend" render="Label" position="255,45" zPosition="2" size="130,125" font="Regular; 35" halign="center" valign="center" transparent="1">
      <convert type="FrontendInfo">SNRdB</convert>
    </widget>
    <eLabel name="snr" text="SNR:" position="455,45" size="100,35" font="Regular; 30" halign="right" transparent="1" />
    <widget source="Frontend" render="Progress" position="565,55" size="278,30" pixmap="skin_default/bar_snr.png" borderColor="uncccccc">
      <convert type="FrontendInfo">SNR</convert>
    </widget>
    <widget source="Frontend" render="Label" position="850,45" size="80,35" font="Regular; 26">
  <convert type="FrontendInfo">SNR</convert>
</widget>
    <eLabel name="lock" text="LOCK:" position="115,105" size="100,44" font="Regular; 30" halign="right" transparent="1" />
    <widget source="Frontend" render="Pixmap" pixmap="skin_default/icons/lock_on.png" position="150,55" zPosition="1" size="38,31" alphatest="on">
      <convert type="FrontendInfo">LOCK</convert>
      <convert type="ConditionalShowHide" />
    </widget>
    <widget source="Frontend" render="Pixmap" pixmap="skin_default/icons/lock_off.png" position="150,55" zPosition="1" size="38,31" alphatest="on">
      <convert type="FrontendInfo">LOCK</convert>
      <convert type="ConditionalShowHide">Invert</convert>
    </widget>
    <eLabel name="agc" text="AGC:" position="455,90" size="100,35" font="Regular; 30" halign="right" transparent="1" />
    <widget source="Frontend" render="Progress" position="565,100" size="278,30" pixmap="skin_default/bar_snr.png" borderColor="uncccccc">
      <convert type="FrontendInfo">AGC</convert>
    </widget>
    <widget source="Frontend" render="Label" position="850,90" size="80,35" font="Regular; 26">
      <convert type="FrontendInfo">AGC</convert>
    </widget>
    <eLabel name="ber" text="BER:" position="455,135" size="100,35" font="Regular; 30" halign="right" transparent="1" />
    <widget source="Frontend" render="Progress" position="565,145" size="278,30" pixmap="skin_default/bar_ber.png" borderColor="uncccccc">
      <convert type="FrontendInfo">BER</convert>
    </widget>
    <widget source="Frontend" render="Label" position="850,135" size="80,35" font="Regular; 26">
      <convert type="FrontendInfo">BER</convert>
    </widget>
    <widget name="config" position="20,195" size="970,550" scrollbarMode="showOnDemand" transparent="1" itemHeight="50" font="Regular; 38" />
    <widget name="introduction" position="20,763" size="970,48" font="Regular; 36" halign="center" foregroundColor="#f8f711" valign="center" />
    <widget name="Cancel" position="85,830" size="250,38" foregroundColor="#ff2525" zPosition="1" transparent="1" font="Regular; 28" halign="center" />
    <widget name="Scan" position="710,830" size="250,38" foregroundColor="green" zPosition="1" transparent="1" font="Regular; 28" halign="center" />
  </screen>"""

	def __init__(self, session):
		self.skin = SignalFinderMultistream.skin
		Screen.__init__(self, session)
		self.initcomplete = False
		try:
			self.session.postScanService = self.session.nav.getCurrentlyPlayingServiceOrGroup()
		except:
			self.session.postScanService = self.session.nav.getCurrentlyPlayingServiceReference()
		nim = self.getNimvalue()
		self.updateSatList()
		self.service = session and session.nav.getCurrentService()
		self.getCurrentTuner = None
		self.stop_service = False
		feinfo = None
		self.tuner = None
		self.DLG = None
		self.frontendData = None
		try:
			self.rotor_pos = config.usage.showdish.value and config.misc.lastrotorposition.value != 9999
		except:
			self.rotor_pos = False
		self["pos"] = Label(_("Current position:"))
		self["status"] = Label("")
		self["Cancel"] = Label(_("Cancel"))
		self["Scan"] = Label(_("Scan"))
		self.tuneTimer = eTimer()
		self.tuneTimer.callback.append(self.updateTuneStatus)
		self.relock_fail_count = 0
		self.relock_restart_threshold = 1
		self.relock_restart_pending = False
		need_sat = False
		if self.service is not None:
			feinfo = self.service.frontendInfo()
			self.frontendData = feinfo and feinfo.getAll(True)
			if self.frontendData:
				type = self.frontendData.get("tuner_type", "UNKNOWN")
				if type == "DVB-S":
					orbital_position = self.frontendData.get("orbital_position", 0)
					if not self.scan_nims.value == "" and len(nim) > 0 and orbital_position != 0:
						tuner = self.frontendData.get("tuner_number", 0)
						get_tuner = False
						for x in nim:
							if int(x[0]) != tuner:
								if not config.misc.direct_tuner.value:
									satList = nimmanager.getSatListForNim(int(x[0]))
									for sat in satList:
										if sat[0] == orbital_position:
											need_sat = True
											self.scan_nims.setValue(x[0])
							else:
								satList = nimmanager.getSatListForNim(int(x[0]))
								for sat in satList:
									if sat[0] == orbital_position:
										get_tuner = True
						if not need_sat and get_tuner:
							need_sat = True
							self.scan_nims.setValue(str(tuner))
		if self.frontendData and not need_sat:
			self.frontendData = None
		if multistream:
			self.createConfig(self.frontendData)
		del feinfo
		del self.service
		self["Frontend"] = FrontendStatus(frontend_source=lambda: self.frontend, update_interval=500)
		self["actions"] = ActionMap(["SetupActions", "MenuActions"],
		{
			"save": self.keyGo,
			"ok": self.keyOK,
			"cancel": self.keyCancel,
			"menu": self.extaMenu,
		}, -2)
		self.list = []
		self.tpslist = []
		self.tpslist_idx = 0
		self["introduction"] = Label("")

		ConfigListScreen.__init__(self, self.list)
		self.feid = None
		if multistream:
			if not self.scan_nims.value == "":
				self.createSetup(self.frontendData)
				self.feid = int(self.scan_nims.value)
				orbpos = "??"
				if len(self.satList) > self.feid and len(self.scan_satselection) > self.feid and len(self.satList[self.feid]):
					orbpos = self.OrbToStr(self.satList[self.feid][self.scan_satselection[self.feid].index][0])
				status_text = orbpos + ": " + str(self.scan_sat.frequency.value) + " " + self.PolToStr(self.scan_sat.polarization.value)
				self["status"].setText(status_text)
		self["config"].onSelectionChanged.append(self.textHelp)
		self.initcomplete = self.feid != None
		self.setTitle(_("Signal finder for DVB-S(S2) tuners") + ": " + plugin_version)
		self.onShow.append(self.initFrontend)

	def extaMenu(self):
		menu_text = _("Set free tuner")
		if not config.misc.direct_tuner.value:
			menu_text = _("Set direct tuner")
		menu = [(menu_text, "setdirect"), (_("Update plugin"), "update")]

		def extraAction(choice):
			if choice is not None:
				if choice[1] == "setdirect":
					self.setDirectTuners()
				elif choice[1] == "update":
					cmd = "chmod 755 %s && %s update" % (loadScript, loadScript)
					text = _("Update plugin")
					self.session.open(Console, text, [cmd])
		self.session.openWithCallback(extraAction, ChoiceBox, title=_("Select action:"), list=menu)

	def setDirectTuners(self):
		text = _("Set free tuner") + "?"
		if not config.misc.direct_tuner.value:
			text = _("Set direct tuner") + "?"
		self.session.openWithCallback(self.setDirectTunersCallback, MessageBox, text, MessageBox.TYPE_YESNO)

	def setDirectTunersCallback(self, answer):
		if answer:
			config.misc.direct_tuner.value = not config.misc.direct_tuner.value
			config.misc.direct_tuner.save()

	def openFrontend(self):
		res_mgr = eDVBResourceManager.getInstance()
		if res_mgr and self.feid != None:
			self.raw_channel = res_mgr.allocateRawChannel(self.feid)
			if self.raw_channel:
				self.frontend = self.raw_channel.getFrontend()
				if self.frontend:
					return True
		return False

	def Exit(self, answer=None):
		self.close()

	def initFrontend(self):
		self.frontend = None
		if not multistream:
			if self.DLG is None:
				self.DLG = self.session.openWithCallback(self.Exit, MessageBox, _("This image not support multistream!"), MessageBox.TYPE_ERROR)
			return
		if not self.openFrontend():
			stop_service = True
			if self.frontendData and not self.stop_service:
				self.getCurrentTuner = self.frontendData and self.frontendData.get("tuner_number", None)
				if self.session.postScanService and self.getCurrentTuner is not None:
					if self.feid is not None and self.feid != self.getCurrentTuner:
						stop_service = False
						for n in nimmanager.nim_slots:
							if hasattr(n, 'config_mode') and n.config_mode in ("loopthrough", "satposdepends"):
								root_id = nimmanager.sec.getRoot(n.slot_id, int(n.config.connectedTo.value))
								if n.config.connectedTo.value and int(n.config.connectedTo.value) == self.feid:
									stop_service = True
							elif hasattr(n, 'config') and hasattr(n.config, 'dvbs') and hasattr(n.config.dvbs, 'configMode') and n.config.dvbs.configMode.value in ("loopthrough", "satposdepends"):
								root_id = nimmanager.sec.getRoot(n.slot_id, int(n.config.dvbs.connectedTo.value))
								if n.config.dvbs.connectedTo.value and int(n.config.dvbs.connectedTo.value) == self.feid:
									stop_service = True
			if self.session.postScanService and stop_service:
				self.session.nav.stopService()
				self.stop_service = True
			if not self.openFrontend():
				if self.session.pipshown:
					if hasattr(self.session, 'infobar'):
						try:
							if self.session.infobar.servicelist and self.session.infobar.servicelist.dopipzap:
								self.session.infobar.servicelist.togglePipzap()
						except:
							pass
					if hasattr(self.session, 'pip'):
						del self.session.pip
					self.session.pipshown = False
				if not self.openFrontend():
					self.deInitFrontend()
					text = _("Sorry, this tuner is in use.")
					if self.session.nav.getRecordings():
						text += "\n"
						text += _("Maybe the reason that recording is currently running.")
					if self.DLG is None:
						self.DLG = self.session.open(MessageBox, text, MessageBox.TYPE_ERROR)
		self.tuner = Tuner(self.frontend)
		self.retune(None)

	def restartFrontendForRelock(self):
		if self.relock_restart_pending:
			return
		self.relock_restart_pending = True
		self.relock_fail_count = 0
		self.tuneTimer.stop()
		self["status"].setText(_("restarting tuner..."))
		try:
			self.session.nav.stopService()
		except:
			pass
		self.stop_service = True
		self.deInitFrontend()
		self.initFrontend()
		self.relock_restart_pending = False

	def deInitFrontend(self):
		if self.DLG:
			try:
				del self.DLG
				self.DLG = None
			except:
				pass
		self.frontend = None
		self.tuner = None
		if hasattr(self, 'raw_channel'):
			del self.raw_channel

	def textHelp(self):
		self["introduction"].setText("")
		cur = self["config"].getCurrent()
		if self.scan_type.value == "predefined_transponder" and self.transpondersEntry is not None and cur == self.transpondersEntry and not self.scan_nims.value == "":
			self["introduction"].setText(_("Left, right or press OK to select transporder."))
		elif not self.scan_nims.value == "":
			self["introduction"].setText(_("Press button OK to start the scan."))
		else:
			if self.scan_nims.value == "":
				self["introduction"].setText(_("Nothing to scan!\nPlease setup your tuner settings before you start a service scan."))

	def updateTuneStatus(self):
		if not self.frontend or not len(self.tpslist):
			return
		stop = False
		dict = {}
		self.frontend.getFrontendStatus(dict)
		state = dict.get("tuner_state")
		if state == "TUNING":
			self.tuneTimer.start(100, True)
		else:
			if state == "LOSTLOCK" or state == "FAILED":
				if len(self.tpslist) > 1:
					self.tpslist_idx += 1
					if self.tpslist_idx >= len(self.tpslist):
						stop = True
						self["status"].setText(_("search failed!"))
						self.tpslist_idx = 0
				else:
					self.relock_fail_count += 1
					if self.relock_fail_count >= self.relock_restart_threshold:
						self.restartFrontendForRelock()
						return
			elif state == "LOCKED":
				if len(self.tpslist) > 1:
					stop = True
				else:
					self.relock_fail_count = 0
					self.tuneTimer.start(250, True)
					return
			else:
				if len(self.tpslist) == 1:
					self.restartFrontendForRelock()
					return
			if not stop:
				self["status"].setText(self.OrbToStr(self.tpslist[self.tpslist_idx][5]) + ": " + str(self.tpslist[self.tpslist_idx][0]) + " " + self.PolToStr(self.tpslist[self.tpslist_idx][2]))
				self.tune(self.tpslist[self.tpslist_idx])
				self.tuneTimer.start(100, True)

	def tune(self, transponder):
		if self.initcomplete:
			if transponder is not None and self.tuner is not None:
				try:
					self.tuner.tune(transponder)
				except Exception as e:
					print(e)

	def retune(self, configElement=None):
		if configElement is None:
			self.tpslist = []
		self.tuneTimer.stop()
		self.relock_fail_count = 0
		if self.scan_nims == []:
			return
		if self.scan_nims.value == "":
			return
		self.tpslist_idx = 0
		tpslist = []
		status_text = ""
		multi_tune = False
		index_to_scan = int(self.scan_nims.value)
		if len(self.satList) <= index_to_scan:
			return
		if len(self.scan_satselection) <= index_to_scan:
			return
		nim = nimmanager.nim_slots[index_to_scan]
		if not nim.isCompatible("DVB-S"):
			return
		nimsats = self.satList[index_to_scan]
		selsatidx = self.scan_satselection[index_to_scan].index
		if self.scan_type.value == "single_transponder":
			if len(nimsats):
				orbpos = nimsats[selsatidx][0]
				if self.scan_sat.system.value == eDVBFrontendParametersSatellite.System_DVB_S:
					fec = self.scan_sat.fec.value
				else:
					fec = self.scan_sat.fec_s2.value
				tpslist.append((self.scan_sat.frequency.value,
						self.scan_sat.symbolrate.value,
						self.scan_sat.polarization.value,
						fec,
						self.scan_sat.inversion.value,
						orbpos,
						self.scan_sat.system.value,
						self.scan_sat.modulation.value,
						self.scan_sat.rolloff.value,
						self.scan_sat.pilot.value,
						self.scan_sat.is_id.value,
						self.scan_sat.pls_mode.value,
						self.scan_sat.pls_code.value))
		elif self.scan_type.value == "predefined_transponder":
			if len(nimsats):
				orbpos = nimsats[selsatidx][0]
				index = self.scan_transponders.index
				if configElement and configElement._value == str(orbpos):
					index = 0
				tps = nimmanager.getTransponders(orbpos)
				if len(tps) > index:
					x = tps[index]
					tpslist.append((x[1] // 1000, x[2] // 1000, x[3], x[4], x[7], orbpos, x[5], x[6], x[8], x[9], x[10], x[11], x[12]))
		elif self.scan_type.value == "single_satellite":
			if len(nimsats):
				multi_tune = True
				orbpos = nimsats[selsatidx][0]
				tps = nimmanager.getTransponders(orbpos)
				for x in tps:
					if x[0] == 0:	#SAT
						tpslist.append((x[1] // 1000, x[2] // 1000, x[3], x[4], x[7], orbpos, x[5], x[6], x[8], x[9], x[10], x[11], x[12]))
		elif "multisat" in self.scan_type.value:
			if len(self.multiscanlist):
				for sat in self.multiscanlist:
					if sat[1].value or len(tpslist) == 0:
						if len(tpslist):
							del tpslist[:]
						tps = nimmanager.getTransponders(sat[0])
						for x in tps:
							if x[0] == 0:	#SAT
								tpslist.append((x[1] // 1000, x[2] // 1000, x[3], x[4], x[7], sat[0], x[5], x[6], x[8], x[9], x[10], x[11], x[12]))
						if sat[1].value:
							multi_tune = True
							break
			else:
				status_text = _("multi scan list empty!")
				SatList = nimmanager.getSatListForNim(index_to_scan)
				for sat in SatList:
					tps = nimmanager.getTransponders(sat[0])
					for x in tps:
						if x[0] == 0:	#SAT
							tpslist.append((x[1] // 1000, x[2] // 1000, x[3], x[4], x[7], sat[0], x[5], x[6], x[8], x[9], x[10], x[11], x[12]))
					if len(tpslist):
						break
		elif self.scan_type.value == "provider":
			if self.provider_list is not None:
				if self.provider_list.value != "none":
					orbpos = 0
					providerList = None
					viasat = False
					kontinent = False
					if self.provider_list.value == "viasat":
						orbpos = 49
						providerList = VIASAT
						viasat = True
					elif self.provider_list.value == "viasat_lat":
						orbpos = 49
						providerList = VIASATLATVIJA
						viasat = True
					elif self.provider_list.value == "xtratv":
						orbpos = 90
						providerList = XTRATV
					elif self.provider_list.value == "viasat_ukr":
						orbpos = 3560
						providerList = VIASATUKR
					elif self.provider_list.value == "ntv":
						orbpos = 360
						providerList = NTVPLUS
					elif self.provider_list.value == "tricolor":
						orbpos = 360
						providerList = TRIKOLOR
					elif self.provider_list.value == "ntv_vostok":
						orbpos = 560
						providerList = NTVPLUS_VOSTOK
					elif self.provider_list.value == "tricolor_sibir":
						orbpos = 560
						providerList = TRIKOLOR_SIBIR
					elif self.provider_list.value == "otautv":
						orbpos = 585
						providerList = OTAUTV
					elif self.provider_list.value == "raduga":
						orbpos = 750
						providerList = RADUGA
					elif self.provider_list.value == "mtstv":
						orbpos = 750
						providerList = MTSTV
					elif self.provider_list.value == "kontinent":
						kontinent = True
						orbpos = 850
						providerList = KONTINENT
					elif self.provider_list.value == "ntv_dalvostok":
						orbpos = 1400
						providerList = NTVPLUS_DAL_VOSTOK
					elif self.provider_list.value == "kontinent_dalvostok":
						orbpos = 1400
						providerList = KONTINENT_DAL_VOSTOK
					if orbpos > 0 and providerList is not None:
						SatList = nimmanager.getSatListForNim(index_to_scan)
						for sat in SatList:
							if sat[0] == orbpos or (viasat and sat[0] in (48, 49)) or (kontinent and sat[0] in (849, 850, 851)):
								tps = nimmanager.getTransponders(sat[0])
								for x in tps:
									pol = x[3]
									if x[3] == 2:
										pol = 0
									if x[3] == 3:
										pol = 1
									if (x[1], pol) in providerList:
										tpslist.append((x[1] // 1000, x[2] // 1000, x[3], x[4], x[7], sat[0], x[5], x[6], x[8], x[9], x[10], x[11], x[12]))
									if len(tpslist):
										multi_tune = True
				elif self.provider_list.value == "none":
					status_text = _("not selected provider!")
		self.tpslist = tpslist
		if len(self.tpslist):
			status_text = self.OrbToStr(self.tpslist[self.tpslist_idx][5]) + ": " + str(self.tpslist[self.tpslist_idx][0]) + " " + self.PolToStr(self.tpslist[self.tpslist_idx][2])
			self.tune(self.tpslist[self.tpslist_idx])
			self.tuneTimer.start(100, True)
		self["status"].setText(status_text)

	def OrbToStr(self, orbpos=-1):
		if orbpos == -1 or orbpos > 3600:
			return "??"
		if orbpos > 1800:
			return "%d.%dW" % ((3600 - orbpos) / 10, (3600 - orbpos) % 10)
		else:
			return "%d.%dE" % (orbpos / 10, orbpos % 10)

	def PolToStr(self, pol):
		return (pol == 0 and "H") or (pol == 1 and "V") or (pol == 2 and "L") or (pol == 3 and "R") or "??"

	def FecToStr(self, fec):
		return (fec == 0 and "Auto") or (fec == 1 and "1/2") or (fec == 2 and "2/3") or (fec == 3 and "3/4") or \
			(fec == 4 and "5/6") or (fec == 5 and "7/8") or (fec == 6 and "8/9") or (fec == 7 and "3/5") or \
			(fec == 8 and "4/5") or (fec == 9 and "9/10") or (fec == 15 and "None") or "Unknown"

	def updateTranspondersList(self, orbpos, tr=None, pol=None):
		if orbpos is not None:
			index = 0
			default = "0"
			list = []
			tps = nimmanager.getTransponders(orbpos)
			for x in tps:
				if x[0] == 0:	#SAT
					s = str(x[1] // 1000) + " " + self.PolToStr(x[3]) + " / " + str(x[2] // 1000) + " / " + self.FecToStr(x[4])
					list.append((str(index), s))
					if tr is not None and tr == x[1] // 1000 and pol is not None and pol == x[3]:
						default = str(index)
					index += 1
			if orbpos == 360 and len(list) >= 20 and default == "0":
				default = "20"
			self.scan_transponders = ConfigSelection(choices=list, default=default)
			self.scan_transponders.addNotifier(self.retune, initial_call=False)

	def updateSatList(self):
		self.satList = []
		for slot in nimmanager.nim_slots:
			if slot.isCompatible("DVB-S"):
				self.satList.append(nimmanager.getSatListForNim(slot.slot))
			else:
				self.satList.append(None)

	def createSetup(self, firstStart=None):
		self.tuneTimer.stop()
		self.list = []
		self.multiscanlist = []
		if self.scan_nims == [] or self.scan_nims.value == "":
			return
		index_to_scan = int(self.scan_nims.value)
		config_list = True
		self.tunerEntry = getConfigListEntry(_("Tuner"), self.scan_nims)
		self.list.append(self.tunerEntry)
		self.typeOfScanEntry = None
		self.systemEntry = None
		self.satelliteEntry = None
		self.modulationEntry = None
		self.transpondersEntry = None
		self.scan_networkScan.value = False
		nim = nimmanager.nim_slots[index_to_scan]
		if nim.isCompatible("DVB-S"):
			self.typeOfScanEntry = getConfigListEntry(_("Type of scan"), self.scan_type)
			self.list.append(self.typeOfScanEntry)
			if self.scan_type.value == "single_transponder":
				self.updateSatList()
				sat = self.satList[index_to_scan][self.scan_satselection[index_to_scan].index]
				self.updateTranspondersList(sat[0])
				if nim.isCompatible("DVB-S2"):
					self.systemEntry = getConfigListEntry(_('System'), self.scan_sat.system)
					self.list.append(self.systemEntry)
				else:
					self.scan_sat.system.value = eDVBFrontendParametersSatellite.System_DVB_S
				self.list.append(getConfigListEntry(_('Satellite'), self.scan_satselection[index_to_scan]))
				self.list.append(getConfigListEntry(_('Frequency'), self.scan_sat.frequency))
				self.list.append(getConfigListEntry(_('Inversion'), self.scan_sat.inversion))
				self.list.append(getConfigListEntry(_('Symbol Rate'), self.scan_sat.symbolrate))
				self.list.append(getConfigListEntry(_("Polarity"), self.scan_sat.polarization))
				if self.scan_sat.system.value == eDVBFrontendParametersSatellite.System_DVB_S:
					self.list.append(getConfigListEntry(_("FEC"), self.scan_sat.fec))
				elif self.scan_sat.system.value == eDVBFrontendParametersSatellite.System_DVB_S2:
					self.list.append(getConfigListEntry(_("FEC"), self.scan_sat.fec_s2))
					self.modulationEntry = getConfigListEntry(_('Modulation'), self.scan_sat.modulation)
					self.list.append(self.modulationEntry)
					self.list.append(getConfigListEntry(_('Rolloff'), self.scan_sat.rolloff))
					self.list.append(getConfigListEntry(_('Pilot'), self.scan_sat.pilot))
					if hasattr(nim, "isMultistream") and nim.isMultistream():
						self.list.append(getConfigListEntry(_('Input Stream ID'), self.scan_sat.is_id))
						self.list.append(getConfigListEntry(_('PLS Mode'), self.scan_sat.pls_mode))
						self.list.append(getConfigListEntry(_('PLS Code'), self.scan_sat.pls_code))
			elif self.scan_type.value == "predefined_transponder":
				self.updateSatList()
				self.satelliteEntry = getConfigListEntry(_('Satellite'), self.scan_satselection[index_to_scan])
				self.list.append(self.satelliteEntry)
				sat = self.satList[index_to_scan][self.scan_satselection[index_to_scan].index]
				if firstStart is not None:
					self.updateTranspondersList(sat[0], tr=self.scan_sat.frequency.value, pol=self.scan_sat.polarization.value)
				else:
					self.updateTranspondersList(sat[0])
				self.transpondersEntry = getConfigListEntry(_('Transponder'), self.scan_transponders)
				self.list.append(self.transpondersEntry)
			elif self.scan_type.value == "single_satellite":
				self.updateSatList()
				sat = self.satList[index_to_scan][self.scan_satselection[index_to_scan].index]
				self.updateTranspondersList(sat[0])
				self.list.append(getConfigListEntry(_("Satellite"), self.scan_satselection[index_to_scan]))
				self.scan_networkScan.value = True
			elif "multisat" in self.scan_type.value:
				tlist = []
				SatList = nimmanager.getSatListForNim(index_to_scan)
				for x in SatList:
					if self.Satexists(tlist, x[0]) == 0:
						tlist.append(x[0])
						sat = ConfigEnableDisable(default=self.scan_type.value.find("_yes") != -1 and True or False)
						configEntry = getConfigListEntry(nimmanager.getSatDescription(x[0]), sat)
						self.list.append(configEntry)
						self.multiscanlist.append((x[0], sat))
						sat.addNotifier(self.retune, initial_call=False)
				self.scan_networkScan.value = True
			elif self.scan_type.value == "provider":
				config_list = False
				satList = nimmanager.getSatListForNim(index_to_scan)
				satchoises = [("none", _("None"))]
				for sat in satList:
					if sat[0] == 48 or sat[0] == 49:
						satchoises.append(("viasat", _("Viasat")))
						satchoises.append(("viasat_lat", _("Viasat Latvija")))
					elif sat[0] == 3560:
						satchoises.append(("viasat_ukr", _("Viasat Ukraine") + " (Amos)"))
					elif sat[0] == 90:
						satchoises.append(("xtratv", _("Xtra TV")))
					elif sat[0] == 360:
						satchoises.append(("ntv", _("NTV Plus")))
						satchoises.append(("tricolor", _("Tricolor TV")))
					elif sat[0] == 560:
						satchoises.append(("ntv_vostok", _("NTV Plus Vostok")))
						satchoises.append(("tricolor_sibir", _("Tricolor TV Sibir")))
					elif sat[0] == 585:
						satchoises.append(("otautv", _("Otau TV")))
					elif sat[0] == 750:
						#satchoises.append(("raduga", _("Raduga TV")))
						satchoises.append(("mtstv", _("MTS TV")))
					elif sat[0] in (849, 850, 851):
						satchoises.append(("kontinent", _("Telekarta (HD)")))
					elif sat[0] == 1400:
						satchoises.append(("ntv_dalvostok", _("NTV Plus Dalniy Vostok")))
						satchoises.append(("kontinent_dalvostok", _("Telekarta-Vostok")))
				self.provider_list = ConfigSelection(default="none", choices=satchoises)
				ProviderEntry = getConfigListEntry(_("Provider"), self.provider_list)
				self.list.append(ProviderEntry)
				self.provider_list.addNotifier(self.retune, initial_call=False)
				if self.provider_list.value == "none":
					self.retune(None)
		self.list.append(getConfigListEntry(_("Network scan"), self.scan_networkScan))
		clear_text = _("Clear before scan")
		if self.scan_type.value == "predefined_transponder" or self.scan_type.value == "single_transponder":
			clear_text += _(" (only this transponder)")
		elif self.scan_type.value == "provider":
			clear_text += _(" (only transponders provider)")
		self.list.append(getConfigListEntry(clear_text, self.scan_clearallservices))
		if config_list:
			self.list.append(getConfigListEntry(_("Only free scan"), self.scan_onlyfree))
		self["config"].list = self.list
		self["config"].l.setList(self.list)

	def Satexists(self, tlist, pos):
		for x in tlist:
			if x == pos:
				return 1
		return 0

	def newConfig(self):
		cur = self["config"].getCurrent()
		if cur is None:
			return
		if cur == self.tunerEntry:
			if not self.scan_nims.value == "":
				self.feid = int(self.scan_nims.value)
				self.deInitFrontend()
				self.initFrontend()
				self.createSetup()
		elif cur == self.typeOfScanEntry or \
			cur == self.systemEntry or \
			cur == self.satelliteEntry or \
			(self.modulationEntry and self.systemEntry[1].value == eDVBFrontendParametersSatellite.System_DVB_S2 and cur == self.modulationEntry):
			self.createSetup()

	def getNimvalue(self):
		nim_list = []
		for n in nimmanager.nim_slots:
			if hasattr(n, 'config_mode'):
				if n.config_mode == "nothing":
					continue
				if hasattr(n, "isFBCLink") and n.isFBCLink():
					continue
				if n.config_mode in ("simple", "equal", "advanced") and len(nimmanager.getSatListForNim(n.slot)) < 1:
					continue
				if n.config_mode in ("loopthrough", "satposdepends"):
					root_id = nimmanager.sec.getRoot(n.slot_id, int(n.config.connectedTo.value))
					if n.type == nimmanager.nim_slots[root_id].type:
						continue
				if n.isCompatible("DVB-S"):
					nim_list.append((str(n.slot), n.friendly_full_description))
			elif hasattr(n, 'config') and hasattr(n.config, 'dvbs') and hasattr(n.config.dvbs, 'configMode'):
				if n.config.dvbs.configMode.value == "nothing":
					continue
				if hasattr(n, "isFBCLink") and n.isFBCLink():
					continue
				if n.config.dvbs.configMode.value in ("simple", "equal", "advanced") and len(nimmanager.getSatListForNim(n.slot)) < 1:
					continue
				if n.config.dvbs.configMode.value in ("loopthrough", "satposdepends"):
					root_id = nimmanager.sec.getRoot(n.slot_id, int(n.config.dvbs.connectedTo.value))
					if n.type == nimmanager.nim_slots[root_id].type:
						continue
				if n.isCompatible("DVB-S"):
					nim_list.append((str(n.slot), n.friendly_full_description))
		self.scan_nims = ConfigSelection(choices=nim_list)
		return nim_list

	def createConfig(self, frontendData):
		defaultSat = {
			"orbpos": 192,
			"system": eDVBFrontendParametersSatellite.System_DVB_S,
			"frequency": 11836,
			"inversion": eDVBFrontendParametersSatellite.Inversion_Unknown,
			"symbolrate": 27500,
			"polarization": eDVBFrontendParametersSatellite.Polarisation_Horizontal,
			"fec": eDVBFrontendParametersSatellite.FEC_Auto,
			"fec_s2": eDVBFrontendParametersSatellite.FEC_9_10,
			"modulation": eDVBFrontendParametersSatellite.Modulation_QPSK,
			"is_id": 0,
			"pls_mode": eDVBFrontendParametersSatellite.PLS_Root,
			"pls_code": 0}

		default_scan = "single_transponder"
		if frontendData is not None:
			ttype = frontendData.get("tuner_type", "UNKNOWN")
			if ttype == "DVB-S":
				defaultSat["system"] = frontendData.get("system", eDVBFrontendParametersSatellite.System_DVB_S)
				defaultSat["frequency"] = frontendData.get("frequency", 0) // 1000
				defaultSat["inversion"] = frontendData.get("inversion", eDVBFrontendParametersSatellite.Inversion_Unknown)
				defaultSat["symbolrate"] = frontendData.get("symbol_rate", 0) // 1000
				defaultSat["polarization"] = frontendData.get("polarization", eDVBFrontendParametersSatellite.Polarisation_Horizontal)
				if defaultSat["system"] == eDVBFrontendParametersSatellite.System_DVB_S2:
					defaultSat["fec_s2"] = frontendData.get("fec_inner", eDVBFrontendParametersSatellite.FEC_Auto)
					defaultSat["rolloff"] = frontendData.get("rolloff", eDVBFrontendParametersSatellite.RollOff_alpha_0_35)
					defaultSat["pilot"] = frontendData.get("pilot", eDVBFrontendParametersSatellite.Pilot_Unknown)
				else:
					defaultSat["fec"] = frontendData.get("fec_inner", eDVBFrontendParametersSatellite.FEC_Auto)
				defaultSat["modulation"] = frontendData.get("modulation", eDVBFrontendParametersSatellite.Modulation_QPSK)
				if 'orbital_position' in frontendData:
					defaultSat["orbpos"] = frontendData['orbital_position']
				defaultSat["is_id"] = frontendData.get("is_id", 0)
				defaultSat["pls_mode"] = frontendData.get("pls_mode", eDVBFrontendParametersSatellite.PLS_Root)
				defaultSat["pls_code"] = frontendData.get("pls_code", 0)
				default_scan = "predefined_transponder"

		self.scan_sat = ConfigSubsection()
		scan_choices = {
		"single_transponder": _("User defined transponder"),
		"predefined_transponder": _("Predefined transponder"),
		"single_satellite": _("Single satellite"),
		"multisat": _("Multisat"),
		"multisat_yes": _("Multisat all select")}
		if self.providersSat():
			scan_choices = {
			"single_transponder": _("User defined transponder"),
			"predefined_transponder": _("Predefined transponder"),
			"single_satellite": _("Single satellite"),
			"multisat": _("Multisat"),
			"multisat_yes": _("Multisat all select"),
			"provider": _("Russian providers")}
		self.scan_type = ConfigSelection(choices=scan_choices, default=default_scan)
		self.scan_transponders = None
		self.provider_list = None
		self.scan_clearallservices = ConfigSelection(default="no", choices=[("no", _("no")), ("yes", _("yes")), ("yes_hold_feeds", _("yes (keep feeds)"))])
		self.scan_onlyfree = ConfigYesNo(default=False)
		self.scan_networkScan = ConfigYesNo(default=False)

		self.scan_sat.system = ConfigSelection(default=defaultSat["system"], choices=[
			(eDVBFrontendParametersSatellite.System_DVB_S, _("DVB-S")),
			(eDVBFrontendParametersSatellite.System_DVB_S2, _("DVB-S2"))])
		self.scan_sat.frequency = ConfigInteger(default=defaultSat["frequency"], limits=(1, 99999))
		self.scan_sat.inversion = ConfigSelection(default=defaultSat["inversion"], choices=[
			(eDVBFrontendParametersSatellite.Inversion_Off, _("Off")),
			(eDVBFrontendParametersSatellite.Inversion_On, _("On")),
			(eDVBFrontendParametersSatellite.Inversion_Unknown, _("Auto"))])
		self.scan_sat.symbolrate = ConfigInteger(default=defaultSat["symbolrate"], limits=(1, 99999))
		self.scan_sat.polarization = ConfigSelection(default=defaultSat["polarization"], choices=[
			(eDVBFrontendParametersSatellite.Polarisation_Horizontal, _("horizontal")),
			(eDVBFrontendParametersSatellite.Polarisation_Vertical, _("vertical")),
			(eDVBFrontendParametersSatellite.Polarisation_CircularLeft, _("circular left")),
			(eDVBFrontendParametersSatellite.Polarisation_CircularRight, _("circular right"))])
		self.scan_sat.fec = ConfigSelection(default=defaultSat["fec"], choices=[
			(eDVBFrontendParametersSatellite.FEC_Auto, _("Auto")),
			(eDVBFrontendParametersSatellite.FEC_1_2, "1/2"),
			(eDVBFrontendParametersSatellite.FEC_2_3, "2/3"),
			(eDVBFrontendParametersSatellite.FEC_3_4, "3/4"),
			(eDVBFrontendParametersSatellite.FEC_5_6, "5/6"),
			(eDVBFrontendParametersSatellite.FEC_7_8, "7/8"),
			(eDVBFrontendParametersSatellite.FEC_None, _("None"))])
		self.scan_sat.fec_s2 = ConfigSelection(default=defaultSat["fec_s2"], choices=[
			(eDVBFrontendParametersSatellite.FEC_Auto, _("Auto")),
			(eDVBFrontendParametersSatellite.FEC_1_2, "1/2"),
			(eDVBFrontendParametersSatellite.FEC_2_3, "2/3"),
			(eDVBFrontendParametersSatellite.FEC_3_4, "3/4"),
			(eDVBFrontendParametersSatellite.FEC_3_5, "3/5"),
			(eDVBFrontendParametersSatellite.FEC_4_5, "4/5"),
			(eDVBFrontendParametersSatellite.FEC_5_6, "5/6"),
			(eDVBFrontendParametersSatellite.FEC_7_8, "7/8"),
			(eDVBFrontendParametersSatellite.FEC_8_9, "8/9"),
			(eDVBFrontendParametersSatellite.FEC_9_10, "9/10")])
		lst = [(eDVBFrontendParametersSatellite.Modulation_QPSK, "QPSK"), (eDVBFrontendParametersSatellite.Modulation_8PSK, "8PSK")]
		if hasattr(eDVBFrontendParametersSatellite, "Modulation_16APSK") and hasattr(eDVBFrontendParametersSatellite, "Modulation_32APSK"):
			lst += [(eDVBFrontendParametersSatellite.Modulation_16APSK, "16APSK"), (eDVBFrontendParametersSatellite.Modulation_32APSK, "32APSK")]
		self.scan_sat.modulation = ConfigSelection(default=defaultSat["modulation"], choices=lst)
		lst = [(eDVBFrontendParametersSatellite.RollOff_alpha_0_35, "0.35"), (eDVBFrontendParametersSatellite.RollOff_alpha_0_25, "0.25"), (eDVBFrontendParametersSatellite.RollOff_alpha_0_20, "0.20")]
		if hasattr(eDVBFrontendParametersSatellite, "RollOff_auto"):
			lst += [(eDVBFrontendParametersSatellite.RollOff_auto, _("Auto"))]
		self.scan_sat.rolloff = ConfigSelection(default=defaultSat.get("rolloff", eDVBFrontendParametersSatellite.RollOff_alpha_0_35), choices=lst)
		self.scan_sat.pilot = ConfigSelection(default=defaultSat.get("pilot", eDVBFrontendParametersSatellite.Pilot_Unknown), choices=[
			(eDVBFrontendParametersSatellite.Pilot_Off, _("Off")),
			(eDVBFrontendParametersSatellite.Pilot_On, _("On")),
			(eDVBFrontendParametersSatellite.Pilot_Unknown, _("Auto"))])
		self.scan_sat.is_id = ConfigInteger(default=defaultSat["is_id"], limits=(0, 255))
		self.scan_sat.pls_mode = ConfigSelection(default=defaultSat["pls_mode"], choices=[
			(eDVBFrontendParametersSatellite.PLS_Root, _("Root")),
			(eDVBFrontendParametersSatellite.PLS_Gold, _("Gold")),
			(eDVBFrontendParametersSatellite.PLS_Combo, _("Combo"))])
		self.scan_sat.pls_code = ConfigInteger(default=defaultSat.get("pls_code", 1), limits=(0, 262142))

		self.scan_scansat = {}
		for sat in nimmanager.satList:
			self.scan_scansat[sat[0]] = ConfigYesNo(default=False)

		self.scan_satselection = []
		for slot in nimmanager.nim_slots:
			if slot.isCompatible("DVB-S"):
				x = getConfigSatlist(defaultSat["orbpos"], self.satList[slot.slot])
				x.addNotifier(self.retune, initial_call=False)
				self.scan_satselection.append(x)
			else:
				self.scan_satselection.append(None)

		for x in (self.scan_nims, self.scan_type, self.scan_sat.frequency,
			self.scan_sat.inversion, self.scan_sat.symbolrate,
			self.scan_sat.polarization, self.scan_sat.fec, self.scan_sat.pilot,
			self.scan_sat.fec_s2, self.scan_sat.fec, self.scan_sat.modulation,
			self.scan_sat.is_id, self.scan_sat.pls_mode, self.scan_sat.pls_code,
			self.scan_sat.rolloff, self.scan_sat.system):
			x.addNotifier(self.retune, initial_call=False)

	def keyLeft(self):
		ConfigListScreen.keyLeft(self)
		self.newConfig()

	def keyRight(self):
		ConfigListScreen.keyRight(self)
		self.newConfig()

	def addSatTransponder(self, tlist, frequency, symbol_rate, polarisation, fec, inversion, orbital_position, system, modulation, rolloff, pilot, is_id, pls_mode, pls_code):
		#print("Add Sat: frequency: " + str(frequency) + " symbol: " + str(symbol_rate) + " pol: " + str(polarisation) + " fec: " + str(fec) + " inversion: " + str(inversion) + " modulation: " + str(modulation) + " system: " + str(system) + " rolloff" + str(rolloff) + " pilot" + str(pilot) + " is_id" + str(is_id) + " pls_mode" + str(pls_mode) + " pls_code" + str(pls_code))
		parm = eDVBFrontendParametersSatellite()
		parm.modulation = modulation
		parm.system = system
		parm.frequency = frequency * 1000
		parm.symbol_rate = symbol_rate * 1000
		parm.polarisation = polarisation
		parm.fec = fec
		parm.inversion = inversion
		parm.orbital_position = orbital_position
		parm.rolloff = rolloff
		parm.pilot = pilot
		parm.is_id = is_id
		parm.pls_mode = pls_mode
		parm.pls_code = pls_code
		tlist.append(parm)

	def getInitialTransponderList(self, tlist, pos):
		list = nimmanager.getTransponders(pos)
		for x in list:
			if x[0] == 0:
				parm = eDVBFrontendParametersSatellite()
				parm.frequency = x[1]
				parm.symbol_rate = x[2]
				parm.polarisation = x[3]
				parm.fec = x[4]
				parm.inversion = x[7]
				parm.orbital_position = pos
				parm.system = x[5]
				parm.modulation = x[6]
				parm.rolloff = x[8]
				parm.pilot = x[9]
				parm.is_id = x[10]
				parm.pls_mode = x[11]
				parm.pls_code = x[12]
				tlist.append(parm)

	def getInitialTransponderProviderList(self, tlist, pos, providers=None):
		list = nimmanager.getTransponders(pos)
		for x in list:
			pol = x[3]
			if x[3] == 2:
				pol = 0
			if x[3] == 3:
				pol = 1
			if x[0] == 0 and providers is not None and (x[1], pol) in providers:
				fec = x[4]
				system = x[5]
				if providers is TRIKOLOR and x[1] == 12360000 and (x[3] == 3 or x[3] == 1):
					fec = 0
					system = 1
				elif providers is KONTINENT and x[1] == 11960000 and x[3] == 0:
					fec = 2
				parm = eDVBFrontendParametersSatellite()
				parm.frequency = x[1]
				parm.symbol_rate = x[2]
				parm.polarisation = x[3]
				parm.fec = fec
				parm.inversion = x[7]
				parm.orbital_position = pos
				parm.system = system
				parm.modulation = x[6]
				parm.rolloff = x[8]
				parm.pilot = x[9]
				parm.is_id = x[10]
				parm.pls_mode = x[11]
				parm.pls_code = x[12]
				tlist.append(parm)

	def providersSat(self):
		providers_sat = False
		for sat in nimmanager.satList:
			if sat[0] == 48 or sat[0] == 49 or sat[0] == 90 or sat[0] == 360 or sat[0] == 560 or sat[0] == 585 or sat[0] == 750 or sat[0] in (849, 850, 851) or sat[0] == 1400 or sat[0] == 3560:
				providers_sat = True
				break
		return providers_sat

	def keyOK(self):
		try:
			cur = self["config"].getCurrent()
			if self.scan_type.value == "predefined_transponder" and self.transpondersEntry is not None and cur == self.transpondersEntry:
				index_to_scan = int(self.scan_nims.value)
				sat = self.satList[index_to_scan][self.scan_satselection[index_to_scan].index]
				if sat[0] is not None:
					tr_list = self.createTranspondersList(sat[0])
					if len(tr_list) > 1:
						orbos = self.OrbToStr(sat[0])
						self.session.openWithCallback(self.configCallback, TranspondersList, list=tr_list, sat=orbos)
			else:
				self.keyGo()
		except:
			pass

	def configCallback(self, callback=None):
		try:
			if callback is not None:
				self.scan_transponders.value = callback
		except:
			pass

	def createTranspondersList(self, orbpos):
		list = []
		index = 0
		tps = nimmanager.getTransponders(orbpos)
		for x in tps:
			if x[0] == 0:
				s = str(x[1] // 1000) + " " + self.PolToStr(x[3]) + " / " + str(x[2] // 1000) + " / " + self.FecToStr(x[4])
				list.append((s, str(index)))
				index += 1
		return list

	def keyGo(self):
		if self.frontend is None:
			text = _("Sorry, this tuner is in use.")
			if self.session.nav.getRecordings():
				text += "\n"
				text += _("Maybe the reason that recording is currently running.")
			self.session.open(MessageBox, text, MessageBox.TYPE_ERROR)
			return
		if self.scan_nims.value == "":
			return
		self.tuneTimer.stop()
		self.deInitFrontend()
		index_to_scan = int(self.scan_nims.value)
		self.feid = index_to_scan
		tlist = []
		flags = None
		startScan = True
		removeAll = True
		if self.scan_nims == []:
			self.session.open(MessageBox, _("No tuner is enabled!\nPlease setup your tuner settings before you start a service scan."), MessageBox.TYPE_ERROR)
			return
		nim = nimmanager.nim_slots[index_to_scan]
		if not nim.isCompatible("DVB-S"):
			return
		if self.scan_type.value.find("_transponder") != -1:
			assert len(self.satList) > index_to_scan
			assert len(self.scan_satselection) > index_to_scan
			nimsats = self.satList[index_to_scan]
			selsatidx = self.scan_satselection[index_to_scan].index
			if len(nimsats):
				orbpos = nimsats[selsatidx][0]
				if self.scan_type.value == "single_transponder":
					if self.scan_sat.system.value == eDVBFrontendParametersSatellite.System_DVB_S:
						fec = self.scan_sat.fec.value
					else:
						fec = self.scan_sat.fec_s2.value
					self.addSatTransponder(tlist, self.scan_sat.frequency.value,
								self.scan_sat.symbolrate.value,
								self.scan_sat.polarization.value,
								fec,
								self.scan_sat.inversion.value,
								orbpos,
								self.scan_sat.system.value,
								self.scan_sat.modulation.value,
								self.scan_sat.rolloff.value,
								self.scan_sat.pilot.value,
								self.scan_sat.is_id.value,
								self.scan_sat.pls_mode.value,
								self.scan_sat.pls_code.value)
				elif self.scan_type.value == "predefined_transponder":
					tps = nimmanager.getTransponders(orbpos)
					if len(tps) > self.scan_transponders.index:
						x = tps[self.scan_transponders.index]
						self.addSatTransponder(tlist, x[1] // 1000, x[2] // 1000, x[3], x[4], x[7], orbpos, x[5], x[6], x[8], x[9], x[10], x[11], x[12])
			removeAll = False
		elif self.scan_type.value == "single_satellite":
			sat = self.satList[index_to_scan][self.scan_satselection[index_to_scan].index]
			self.getInitialTransponderList(tlist, sat[0])
		elif self.scan_type.value.find("multisat") != -1:
			SatList = nimmanager.getSatListForNim(index_to_scan)
			for x in self.multiscanlist:
				if x[1].value:
					self.getInitialTransponderList(tlist, x[0])
		elif self.scan_type.value == "provider":
			if self.provider_list is not None:
				if self.provider_list.value == "none":
					startScan = False
				else:
					orbpos = 0
					startScan = False
					providerList = None
					viasat = False
					kontinent = False
					if self.provider_list.value == "viasat":
						orbpos = 49
						providerList = VIASAT
						viasat = True
					elif self.provider_list.value == "viasat_lat":
						orbpos = 49
						providerList = VIASATLATVIJA
						viasat = True
					elif self.provider_list.value == "xtratv":
						orbpos = 90
						providerList = XTRATV
					elif self.provider_list.value == "viasat_ukr":
						orbpos = 3560
						providerList = VIASATUKR
					elif self.provider_list.value == "ntv":
						orbpos = 360
						providerList = NTVPLUS
					elif self.provider_list.value == "tricolor":
						orbpos = 360
						providerList = TRIKOLOR
					elif self.provider_list.value == "ntv_vostok":
						orbpos = 560
						providerList = NTVPLUS_VOSTOK
					elif self.provider_list.value == "tricolor_sibir":
						orbpos = 560
						providerList = TRIKOLOR_SIBIR
					elif self.provider_list.value == "otautv":
						orbpos = 585
						providerList = OTAUTV
					elif self.provider_list.value == "raduga":
						orbpos = 750
						providerList = RADUGA
					elif self.provider_list.value == "mtstv":
						orbpos = 750
						providerList = MTSTV
					elif self.provider_list.value == "kontinent":
						orbpos = 850
						providerList = KONTINENT
						kontinent = True
					elif self.provider_list.value == "ntv_dalvostok":
						orbpos = 1400
						providerList = NTVPLUS_DAL_VOSTOK
					elif self.provider_list.value == "kontinent_dalvostok":
						orbpos = 1400
						providerList = KONTINENT_DAL_VOSTOK
					if orbpos > 0 and providerList is not None:
						SatList = nimmanager.getSatListForNim(index_to_scan)
						for sat in SatList:
							if sat[0] == orbpos or (viasat and sat[0] in (48, 49)) or (kontinent and sat[0] in (849, 850, 851)):
								self.getInitialTransponderProviderList(tlist, sat[0], providers=providerList)
						removeAll = False
						startScan = True

		flags = self.scan_networkScan.value and eComponentScan.scanNetworkSearch or 0
		tmp = self.scan_clearallservices.value
		self.kept_feed_snapshot = None
		if tmp == "yes":
			flags |= eComponentScan.scanRemoveServices
		elif tmp == "yes_hold_feeds":
			current_orb = None
			try:
				# Check which configuration variable holds the satellite position
				if hasattr(self, 'scan_sat') and hasattr(self.scan_sat, 'value'):
					current_orb = int(self.scan_sat.value)
				elif hasattr(self, 'satList') and hasattr(self.satList, 'value'):
					current_orb = int(self.satList.value)
			except:
				pass

			# Pass the current satellite position to only snapshot feeds on THIS sat
			self.kept_feed_snapshot = collectKeepServicesSnapshot(orb_filter=current_orb)
			flags |= eComponentScan.scanRemoveServices

		if tmp == "yes" and not removeAll:
			flags |= eComponentScan.scanDontRemoveUnscanned

		if self.scan_onlyfree.value:
			flags |= eComponentScan.scanOnlyFree

		for x in self["config"].list:
			x[1].save()

		if startScan:
			self.startScan(tlist, flags, index_to_scan)

	def keyCancel(self):
		if self.session.postScanService and self.stop_service:
			self.session.openWithCallback(self.restartPrevService, MessageBox, _("Zap back to service before a service scan?"), MessageBox.TYPE_YESNO, timeout=10)
		else:
			self.restartPrevService(True)

	def restartPrevService(self, answer):
		if self.scan_clearallservices.value == "yes_hold_feeds":
			appendSnapshotServicesToLamedb(getattr(self, "kept_feed_snapshot", None))
			self.kept_feed_snapshot = None
		for x in self["config"].list:
			x[1].cancel()
		self.tuneTimer.stop()
		self.deInitFrontend()
		if answer:
			self.session.nav.playService(self.session.postScanService)
		self.close()

	def handleKeyFileCallback(self, answer):
		ConfigListScreen.handleKeyFileCallback(self, answer)
		self.newConfig()

	def startScan(self, tlist, flags, feid):
		if len(tlist):
			self.session.openWithCallback(self.serviceScanFinished, ServiceScan, [{"transponders": tlist, "feid": feid, "flags": flags}])
		else:
			self.session.open(MessageBox, _("Nothing to scan!\nPlease setup your tuner settings before you start a service scan."), MessageBox.TYPE_ERROR)
			self.keyCancel()

	def serviceScanFinished(self, answer=None):
		if answer is not True:
			self.session.openWithCallback(self.restartSignalFinder, MessageBox, _("Do you want to scan another transponder/satellite?"), MessageBox.TYPE_YESNO, timeout=10)
		elif answer is True:
			self.restartPrevService(True)

	def restartSignalFinder(self, answer):
		if answer:
			self.tuneTimer.stop()
			self.deInitFrontend()
			self.initFrontend()
		else:
			self.keyCancel()

#def SignalFinderMainnew(session, **kwargs):
#	sat_nims = nimmanager.getNimListOfType("DVB-S")
#	ter_nims = nimmanager.getNimListOfType("DVB-T")
#	sat_nimList = []
#	ter_nimList = []
#	for x in sat_nims:
#		if nimmanager.getNimConfig(x).configMode.value in ("loopthrough", "satposdepends", "nothing"):
#			continue
#		if nimmanager.getNimConfig(x).configMode.value in ("simple", "equal","advanced") and len(nimmanager.getSatListForNim(x)) < 1:
#			continue
#		sat_nimList.append(x)
#	flag_disabled = False
#	for x in ter_nims:
#		if nimmanager.getNimConfig(x).configMode.value == "nothing":
#			flag_disabled = True
#		if nimmanager.getNimConfig(x).configMode.value == "enabled":
#			ter_nimList.append(x)
#	sat = len(sat_nimList)
#	ter = len(ter_nimList)
#	message = ""
#	sat_message = _("No satellites configured for DVB-S2 tuner(s).\nPlease check your tuner(s) setup.")
#	ter_message = _("DVB-T(T2) tuner(s) not enabled.\nPlease check your tuner(s) setup.")
#	record = session.nav.getRecordings()
#	error_sat = len(sat_nims) > 0 and sat == 0
#	error_ter = len(ter_nims) > 0 and ter == 0 and flag_disabled
#	if error_sat and ter == 0:
#		message = sat_message
#	elif error_ter and sat == 0:
#		message = ter_message
#	if error_sat and error_ter:
#		message = sat_message + "\n" + ter_message
#	if message != "":
#		session.open(MessageBox, message, MessageBox.TYPE_ERROR)
#		return
#	if record and (sat + ter) == 1:
#		session.open(MessageBox, _("A recording is currently running. Please stop the recording before trying to start a service scan."), MessageBox.TYPE_ERROR)
#		return
#	if sat > 0 and ter == 0:
#		session.open(SignalFinder)
#	elif ter > 0 and sat == 0:
#		pass
#		#session.open(SignalFinderDVBT)
#	elif ter > 0 and sat > 0:
#		menu = [(_("DVB-S2"), "sat"),(_("DVB-T(T2)"), "ter")]
#		def boxAction(choice):
#			if choice is not None:
#				if choice[1] == "sat":
#					session.open(SignalFinder)
#				elif choice[1] == "ter":
#					pass
#					#session.open(SignalFinderDVBT)
#		dlg = session.openWithCallback(boxAction, ChoiceBox, title=_("Select the type of tuner:"), list=menu)
#		dlg.setTitle("DVB-S2/T(T2)")


class SignalFinder(ConfigListScreen, Screen):
	if isHD():
		skin = """
			<screen position="center,center" size="1200,640" title="Signal finder" >
				<widget name="pos" position="10,10" size="210,25" font="Regular;22" halign="right" transparent="1" />
				<widget name="status" position="230,10" size="370,25" font="Regular;22" halign="left" foregroundColor="#f8f711" transparent="1" />
				<widget source="Frontend" render="Progress" pixmap="/usr/lib/enigma2/python/Plugins/SystemPlugins/Signalfinder/image/bar_big.png" position="140,40" size="1000,50" borderColor="#00808888">
					<convert type="FrontendInfo">SNR</convert>
				</widget>
				<eLabel text="SNR:" position="145,50" size="100,35" backgroundColor="#00666666" transparent="1" font="Regular;35" />
				<widget source="Frontend" render="Label" position="910,50" size="225,35" halign="right" transparent="1" font="Regular;35">
					<convert type="FrontendInfo">SNR</convert>
				</widget>
				<widget source="Frontend" render="Progress" pixmap="/usr/lib/enigma2/python/Plugins/SystemPlugins/Signalfinder/image/bar_agc.png" position="140,100" size="1000,50" borderColor="#00808888">
					<convert type="FrontendInfo">AGC</convert>
				</widget>
				<eLabel text="AGC:" position="145,110" size="100,35" backgroundColor="#00666666" transparent="1" font="Regular;35" />
				<widget source="Frontend" render="Label" position="910,110" size="225,35" halign="right" transparent="1" font="Regular;35">
					<convert type="FrontendInfo">AGC</convert>
				</widget>
				<widget source="Frontend" render="Progress" pixmap="/usr/lib/enigma2/python/Plugins/SystemPlugins/Signalfinder/image/bar_ber.png" position="140,160" size="1000,50" borderColor="#00808888">
					<convert type="FrontendInfo">BER</convert>
				</widget>
				<eLabel text="BER:" position="145,170" size="100,35" backgroundColor="#00666666"  transparent="1" font="Regular;35" />
				<widget source="Frontend" render="Label" position="910,170" size="225,35" halign="right" transparent="1" font="Regular;35">
					<convert type="FrontendInfo">BER</convert>
				</widget>
				<eLabel text="SNR:" position="140,225" size="120,20" backgroundColor="#00666666" transparent="1" zPosition="5" font="Regular;18" />
				<widget source="Frontend" render="Label" position="140,245" size="310,75" font="Regular;72" halign="left" backgroundColor="#00666666" transparent="1">
					<convert type="FrontendInfo">SNRdB</convert>
				</widget>
				<eLabel text="AGC:" position="140,325" size="120,20" backgroundColor="#00666666" transparent="1" zPosition="5" font="Regular;18" />
				<widget source="Frontend" render="Label" position="140,345" size="310,75" backgroundColor="#00666666" transparent="1" font="Regular;72" halign="left">
					<convert type="FrontendInfo">AGC</convert>
				</widget>
				<eLabel text="BER:" position="140,425" size="120,20" backgroundColor="#00666666" transparent="1" zPosition="5" font="Regular;18" />
				<widget source="Frontend" render="Label" position="140,445" size="310,75" font="Regular;72" halign="left" backgroundColor="#00666666" transparent="1">
					<convert type="FrontendInfo">BER</convert>
				</widget>
				<widget text="LOCK" source="Frontend" render="FixedLabel" position="140,525" size="310,90" font="Regular;72" halign="left" foregroundColor="#00389416" backgroundColor="#00666666" transparent="1" >
					<convert type="FrontendInfo">LOCK</convert>
					<convert type="ConditionalShowHide" />
				</widget>
				<widget name="config" position="420,225" size="720,396" transparent="1" scrollbarMode="showOnDemand" />
				<widget name="introduction" position="540,10" size="600,30"  zPosition="1" transparent="1" foregroundColor="#f8f711" font="Regular;22" />
				<widget name="Cancel" position="200,610" size="250,28" foregroundColor="#00ff2525" zPosition="1" transparent="1" font="Regular;24" />
				<widget name="Scan" position="750,610" size="250,28" foregroundColor="#00389416" zPosition="1" transparent="1" font="Regular;24" />
			</screen>"""
	else:
		skin = """
  <screen position="center,center" size="1020,900" title="Signal finder">
    <widget name="pos" position="20,5" size="300,33" font="Regular; 26" halign="right" transparent="1" />
    <widget name="status" position="325,5" size="650,33" font="Regular; 26" halign="left" foregroundColor="#f8f711" transparent="1" />
    <widget source="Frontend" render="Label" position="255,45" zPosition="2" size="130,125" font="Regular; 35" halign="center" valign="center" transparent="1">
      <convert type="FrontendInfo">SNRdB</convert>
    </widget>
    <eLabel name="snr" text="SNR:" position="455,45" size="100,35" font="Regular; 30" halign="right" transparent="1" />
    <widget source="Frontend" render="Progress" position="565,55" size="278,30" pixmap="skin_default/bar_snr.png" borderColor="uncccccc">
      <convert type="FrontendInfo">SNR</convert>
    </widget>
    <widget source="Frontend" render="Label" position="850,45" size="80,35" font="Regular; 26">
  <convert type="FrontendInfo">SNR</convert>
</widget>
    <eLabel name="lock" text="LOCK:" position="115,105" size="100,44" font="Regular; 30" halign="right" transparent="1" />
    <widget source="Frontend" render="Pixmap" pixmap="skin_default/icons/lock_on.png" position="150,55" zPosition="1" size="38,31" alphatest="on">
      <convert type="FrontendInfo">LOCK</convert>
      <convert type="ConditionalShowHide" />
    </widget>
    <widget source="Frontend" render="Pixmap" pixmap="skin_default/icons/lock_off.png" position="150,55" zPosition="1" size="38,31" alphatest="on">
      <convert type="FrontendInfo">LOCK</convert>
      <convert type="ConditionalShowHide">Invert</convert>
    </widget>
    <eLabel name="agc" text="AGC:" position="455,90" size="100,35" font="Regular; 30" halign="right" transparent="1" />
    <widget source="Frontend" render="Progress" position="565,100" size="278,30" pixmap="skin_default/bar_snr.png" borderColor="uncccccc">
      <convert type="FrontendInfo">AGC</convert>
    </widget>
    <widget source="Frontend" render="Label" position="850,90" size="80,35" font="Regular; 26">
      <convert type="FrontendInfo">AGC</convert>
    </widget>
    <eLabel name="ber" text="BER:" position="455,135" size="100,35" font="Regular; 30" halign="right" transparent="1" />
    <widget source="Frontend" render="Progress" position="565,145" size="278,30" pixmap="skin_default/bar_ber.png" borderColor="uncccccc">
      <convert type="FrontendInfo">BER</convert>
    </widget>
    <widget source="Frontend" render="Label" position="850,135" size="80,35" font="Regular; 26">
      <convert type="FrontendInfo">BER</convert>
    </widget>
    <widget name="config" position="20,195" size="970,550" scrollbarMode="showOnDemand" transparent="1" itemHeight="50" font="Regular; 38" />
    <widget name="introduction" position="20,763" size="970,48" font="Regular; 36" halign="center" foregroundColor="#f8f711" valign="center" />
    <widget name="Cancel" position="85,830" size="250,38" foregroundColor="#ff2525" zPosition="1" transparent="1" font="Regular; 28" halign="center" />
    <widget name="Scan" position="710,830" size="250,38" foregroundColor="green" zPosition="1" transparent="1" font="Regular; 28" halign="center" />
  </screen>"""

	def __init__(self, session):
		self.skin = SignalFinder.skin
		Screen.__init__(self, session)
		self.initcomplete = False
		try:
			self.session.postScanService = self.session.nav.getCurrentlyPlayingServiceOrGroup()
		except:
			self.session.postScanService = self.session.nav.getCurrentlyPlayingServiceReference()
		nim = self.getNimvalue()
		self.updateSatList()
		self.service = session and session.nav.getCurrentService()
		self.getCurrentTuner = None
		self.stop_service = False
		feinfo = None
		self.tuner = None
		self.DLG = None
		self.frontendData = None
		try:
			self.rotor_pos = config.usage.showdish.value and config.misc.lastrotorposition.value != 9999
		except:
			self.rotor_pos = False
		self["pos"] = Label(_("Current position:"))
		self["status"] = Label("")
		self["Cancel"] = Label(_("Cancel"))
		self["Scan"] = Label(_("Scan"))
		self.tuneTimer = eTimer()
		self.tuneTimer.callback.append(self.updateTuneStatus)
		self.relock_fail_count = 0
		self.relock_restart_threshold = 1
		self.relock_restart_pending = False
		need_sat = False
		if self.service is not None:
			feinfo = self.service.frontendInfo()
			self.frontendData = feinfo and feinfo.getAll(True)
			if self.frontendData:
				type = self.frontendData.get("tuner_type", "UNKNOWN")
				if type == "DVB-S":
					orbital_position = self.frontendData.get("orbital_position", 0)
					if not self.scan_nims.value == "" and len(nim) > 0 and orbital_position != 0:
						tuner = self.frontendData.get("tuner_number", 0)
						get_tuner = False
						for x in nim:
							if int(x[0]) != tuner:
								if not config.misc.direct_tuner.value:
									satList = nimmanager.getSatListForNim(int(x[0]))
									for sat in satList:
										if sat[0] == orbital_position:
											need_sat = True
											self.scan_nims.setValue(x[0])
							else:
								satList = nimmanager.getSatListForNim(int(x[0]))
								for sat in satList:
									if sat[0] == orbital_position:
										get_tuner = True
						if not need_sat and get_tuner:
							need_sat = True
							self.scan_nims.setValue(str(tuner))
		if self.frontendData and not need_sat:
			self.frontendData = None
		self.createConfig(self.frontendData)
		del feinfo
		del self.service
		self["Frontend"] = FrontendStatus(frontend_source=lambda: self.frontend, update_interval=500)
		self["actions"] = ActionMap(["SetupActions", "MenuActions"],
		{
			"save": self.keyGo,
			"ok": self.keyOK,
			"cancel": self.keyCancel,
			"menu": self.extaMenu,
		}, -2)
		self.list = []
		self.tpslist = []
		self.tpslist_idx = 0
		self["introduction"] = Label("")

		ConfigListScreen.__init__(self, self.list)
		if not self.scan_nims.value == "":
			self.createSetup(self.frontendData)
			self.feid = int(self.scan_nims.value)
			orbpos = "??"
			if len(self.satList) > self.feid and len(self.scan_satselection) > self.feid and len(self.satList[self.feid]):
				orbpos = self.OrbToStr(self.satList[self.feid][self.scan_satselection[self.feid].index][0])
			status_text = orbpos + ": " + str(self.scan_sat.frequency.value) + " " + self.PolToStr(self.scan_sat.polarization.value)
			self["status"].setText(status_text)
		else:
			self.feid = None
		self["config"].onSelectionChanged.append(self.textHelp)
		self.initcomplete = self.feid != None
		self.setTitle(_("Signal finder for DVB-S(S2) tuners") + ": " + plugin_version)
		self.onShow.append(self.initFrontend)

	def extaMenu(self):
		menu_text = _("Set free tuner")
		if not config.misc.direct_tuner.value:
			menu_text = _("Set direct tuner")
		menu = [(menu_text, "setdirect"), (_("Update plugin"), "update")]

		def extraAction(choice):
			if choice is not None:
				if choice[1] == "setdirect":
					self.setDirectTuners()
				elif choice[1] == "update":
					cmd = "chmod 755 %s && %s update" % (loadScript, loadScript)
					text = _("Update plugin")
					self.session.open(Console, text, [cmd])
		self.session.openWithCallback(extraAction, ChoiceBox, title=_("Select action:"), list=menu)

	def setDirectTuners(self):
		text = _("Set free tuner") + "?"
		if not config.misc.direct_tuner.value:
			text = _("Set direct tuner") + "?"
		self.session.openWithCallback(self.setDirectTunersCallback, MessageBox, text, MessageBox.TYPE_YESNO)

	def setDirectTunersCallback(self, answer):
		if answer:
			config.misc.direct_tuner.value = not config.misc.direct_tuner.value
			config.misc.direct_tuner.save()

	def openFrontend(self):
		res_mgr = eDVBResourceManager.getInstance()
		if res_mgr and self.feid != None:
			self.raw_channel = res_mgr.allocateRawChannel(self.feid)
			if self.raw_channel:
				self.frontend = self.raw_channel.getFrontend()
				if self.frontend:
					return True
		return False

	def initFrontend(self):
		self.frontend = None
		if not self.openFrontend():
			stop_service = True
			if self.frontendData and not self.stop_service:
				self.getCurrentTuner = self.frontendData and self.frontendData.get("tuner_number", None)
				if self.session.postScanService and self.getCurrentTuner is not None:
					if self.feid is not None and self.feid != self.getCurrentTuner:
						stop_service = False
						for n in nimmanager.nim_slots:
							if hasattr(n, 'config_mode') and n.config_mode in ("loopthrough", "satposdepends"):
								root_id = nimmanager.sec.getRoot(n.slot_id, int(n.config.connectedTo.value))
								if n.config.connectedTo.value and int(n.config.connectedTo.value) == self.feid:
									stop_service = True
							elif hasattr(n, 'config') and hasattr(n.config, 'dvbs') and hasattr(n.config.dvbs, 'configMode') and n.config.dvbs.configMode.value in ("loopthrough", "satposdepends"):
								root_id = nimmanager.sec.getRoot(n.slot_id, int(n.config.dvbs.connectedTo.value))
								if n.config.dvbs.connectedTo.value and int(n.config.dvbs.connectedTo.value) == self.feid:
									stop_service = True
			if self.session.postScanService and stop_service:
				self.session.nav.stopService()
				self.stop_service = True
			if not self.openFrontend():
				if self.session.pipshown:
					if hasattr(self.session, 'infobar'):
						try:
							if self.session.infobar.servicelist and self.session.infobar.servicelist.dopipzap:
								self.session.infobar.servicelist.togglePipzap()
						except:
							pass
					if hasattr(self.session, 'pip'):
						del self.session.pip
					self.session.pipshown = False
				if not self.openFrontend():
					self.deInitFrontend()
					text = _("Sorry, this tuner is in use.")
					if self.session.nav.getRecordings():
						text += "\n"
						text += _("Maybe the reason that recording is currently running.")
					if self.DLG is None:
						self.DLG = self.session.open(MessageBox, text, MessageBox.TYPE_ERROR)
		self.tuner = Tuner(self.frontend)
		self.retune(None)

	def restartFrontendForRelock(self):
		if self.relock_restart_pending:
			return
		self.relock_restart_pending = True
		self.relock_fail_count = 0
		self.tuneTimer.stop()
		self["status"].setText(_("restarting tuner..."))
		try:
			self.session.nav.stopService()
		except:
			pass
		self.stop_service = True
		self.deInitFrontend()
		self.initFrontend()
		self.relock_restart_pending = False

	def deInitFrontend(self):
		if self.DLG:
			try:
				del self.DLG
				self.DLG = None
			except:
				pass
		self.frontend = None
		self.tuner = None
		if hasattr(self, 'raw_channel'):
			del self.raw_channel

	def textHelp(self):
		self["introduction"].setText("")
		cur = self["config"].getCurrent()
		if self.scan_type.value == "predefined_transponder" and self.transpondersEntry is not None and cur == self.transpondersEntry and not self.scan_nims.value == "":
			self["introduction"].setText(_("Left, right or press OK to select transporder."))
		elif not self.scan_nims.value == "":
			self["introduction"].setText(_("Press button OK to start the scan."))
		else:
			if self.scan_nims.value == "":
				self["introduction"].setText(_("Nothing to scan!\nPlease setup your tuner settings before you start a service scan."))

	def updateTuneStatus(self):
		if not self.frontend or not len(self.tpslist):
			return
		stop = False
		dict = {}
		self.frontend.getFrontendStatus(dict)
		state = dict.get("tuner_state")
		if state == "TUNING":
			self.tuneTimer.start(100, True)
		else:
			if state == "LOSTLOCK" or state == "FAILED":
				if len(self.tpslist) > 1:
					self.tpslist_idx += 1
					if self.tpslist_idx >= len(self.tpslist):
						stop = True
						self["status"].setText(_("search failed!"))
						self.tpslist_idx = 0
				else:
					self.relock_fail_count += 1
					if self.relock_fail_count >= self.relock_restart_threshold:
						self.restartFrontendForRelock()
						return
			elif state == "LOCKED":
				if len(self.tpslist) > 1:
					stop = True
				else:
					self.relock_fail_count = 0
					self.tuneTimer.start(250, True)
					return
			else:
				if len(self.tpslist) == 1:
					self.restartFrontendForRelock()
					return
			if not stop:
				self["status"].setText(self.OrbToStr(self.tpslist[self.tpslist_idx][5]) + ": " + str(self.tpslist[self.tpslist_idx][0]) + " " + self.PolToStr(self.tpslist[self.tpslist_idx][2]))
				self.tune(self.tpslist[self.tpslist_idx])
				self.tuneTimer.start(100, True)

	def tune(self, transponder):
		if self.initcomplete:
			if transponder is not None and self.tuner is not None:
				try:
					self.tuner.tune(transponder)
				except Exception as e:
					print(e)

	def retune(self, configElement=None):
		if configElement is None:
			self.tpslist = []
		self.tuneTimer.stop()
		self.relock_fail_count = 0
		if self.scan_nims == []:
			return
		if self.scan_nims.value == "":
			return
		self.tpslist_idx = 0
		tpslist = []
		status_text = ""
		multi_tune = False
		index_to_scan = int(self.scan_nims.value)
		if len(self.satList) <= index_to_scan:
			return
		if len(self.scan_satselection) <= index_to_scan:
			return
		nim = nimmanager.nim_slots[index_to_scan]
		if not nim.isCompatible("DVB-S"):
			return
		nimsats = self.satList[index_to_scan]
		selsatidx = self.scan_satselection[index_to_scan].index
		if self.scan_type.value == "single_transponder":
			if len(nimsats):
				orbpos = nimsats[selsatidx][0]
				if self.scan_sat.system.value == eDVBFrontendParametersSatellite.System_DVB_S:
					fec = self.scan_sat.fec.value
				else:
					fec = self.scan_sat.fec_s2.value
				tpslist.append((self.scan_sat.frequency.value,
						self.scan_sat.symbolrate.value,
						self.scan_sat.polarization.value,
						fec,
						self.scan_sat.inversion.value,
						orbpos,
						self.scan_sat.system.value,
						self.scan_sat.modulation.value,
						self.scan_sat.rolloff.value,
						self.scan_sat.pilot.value))
		elif self.scan_type.value == "predefined_transponder":
			if len(nimsats):
				orbpos = nimsats[selsatidx][0]
				index = self.scan_transponders.index
				if configElement and configElement._value == str(orbpos):
					index = 0
				tps = nimmanager.getTransponders(orbpos)
				if len(tps) > index:
					x = tps[index]
					tpslist.append((x[1] // 1000, x[2] // 1000, x[3], x[4], x[7], orbpos, x[5], x[6], x[8], x[9]))
		elif self.scan_type.value == "single_satellite":
			if len(nimsats):
				multi_tune = True
				orbpos = nimsats[selsatidx][0]
				tps = nimmanager.getTransponders(orbpos)
				for x in tps:
					if x[0] == 0:	#SAT
						tpslist.append((x[1] // 1000, x[2] // 1000, x[3], x[4], x[7], orbpos, x[5], x[6], x[8], x[9]))
		elif "multisat" in self.scan_type.value:
			if len(self.multiscanlist):
				for sat in self.multiscanlist:
					if sat[1].value or len(tpslist) == 0:
						if len(tpslist):
							del tpslist[:]
						tps = nimmanager.getTransponders(sat[0])
						for x in tps:
							if x[0] == 0:	#SAT
								tpslist.append((x[1] // 1000, x[2] // 1000, x[3], x[4], x[7], sat[0], x[5], x[6], x[8], x[9]))
						if sat[1].value:
							multi_tune = True
							break
			else:
				status_text = _("multi scan list empty!")
				SatList = nimmanager.getSatListForNim(index_to_scan)
				for sat in SatList:
					tps = nimmanager.getTransponders(sat[0])
					for x in tps:
						if x[0] == 0:	#SAT
							tpslist.append((x[1] // 1000, x[2] // 1000, x[3], x[4], x[7], sat[0], x[5], x[6], x[8], x[9]))
					if len(tpslist):
						break
		elif self.scan_type.value == "provider":
			if self.provider_list is not None:
				if self.provider_list.value != "none":
					orbpos = 0
					providerList = None
					viasat = False
					kontinent = False
					if self.provider_list.value == "viasat":
						orbpos = 49
						providerList = VIASAT
						viasat = True
					elif self.provider_list.value == "viasat_lat":
						orbpos = 49
						providerList = VIASATLATVIJA
						viasat = True
					elif self.provider_list.value == "xtratv":
						orbpos = 90
						providerList = XTRATV
					elif self.provider_list.value == "viasat_ukr":
						orbpos = 3560
						providerList = VIASATUKR
					elif self.provider_list.value == "ntv":
						orbpos = 360
						providerList = NTVPLUS
					elif self.provider_list.value == "tricolor":
						orbpos = 360
						providerList = TRIKOLOR
					elif self.provider_list.value == "ntv_vostok":
						orbpos = 560
						providerList = NTVPLUS_VOSTOK
					elif self.provider_list.value == "tricolor_sibir":
						orbpos = 560
						providerList = TRIKOLOR_SIBIR
					elif self.provider_list.value == "otautv":
						orbpos = 585
						providerList = OTAUTV
					elif self.provider_list.value == "raduga":
						orbpos = 750
						providerList = RADUGA
					elif self.provider_list.value == "mtstv":
						orbpos = 750
						providerList = MTSTV
					elif self.provider_list.value == "kontinent":
						kontinent = True
						orbpos = 850
						providerList = KONTINENT
					elif self.provider_list.value == "ntv_dalvostok":
						orbpos = 1400
						providerList = NTVPLUS_DAL_VOSTOK
					elif self.provider_list.value == "kontinent_dalvostok":
						orbpos = 1400
						providerList = KONTINENT_DAL_VOSTOK
					if orbpos > 0 and providerList is not None:
						SatList = nimmanager.getSatListForNim(index_to_scan)
						for sat in SatList:
							if sat[0] == orbpos or (viasat and sat[0] in (48, 49)) or (kontinent and sat[0] in (849, 850, 851)):
								tps = nimmanager.getTransponders(sat[0])
								for x in tps:
									pol = x[3]
									if x[3] == 2:
										pol = 0
									if x[3] == 3:
										pol = 1
									if (x[1], pol) in providerList:
										tpslist.append((x[1] // 1000, x[2] // 1000, x[3], x[4], x[7], sat[0], x[5], x[6], x[8], x[9]))
									if len(tpslist):
										multi_tune = True
				elif self.provider_list.value == "none":
					status_text = _("not selected provider!")
		self.tpslist = tpslist
		if len(self.tpslist):
			status_text = self.OrbToStr(self.tpslist[self.tpslist_idx][5]) + ": " + str(self.tpslist[self.tpslist_idx][0]) + " " + self.PolToStr(self.tpslist[self.tpslist_idx][2])
			self.tune(self.tpslist[self.tpslist_idx])
			self.tuneTimer.start(100, True)
		self["status"].setText(status_text)

	def OrbToStr(self, orbpos=-1):
		if orbpos == -1 or orbpos > 3600:
			return "??"
		if orbpos > 1800:
			return "%d.%dW" % ((3600 - orbpos) / 10, (3600 - orbpos) % 10)
		else:
			return "%d.%dE" % (orbpos / 10, orbpos % 10)

	def PolToStr(self, pol):
		return (pol == 0 and "H") or (pol == 1 and "V") or (pol == 2 and "L") or (pol == 3 and "R") or "??"

	def FecToStr(self, fec):
		return (fec == 0 and "Auto") or (fec == 1 and "1/2") or (fec == 2 and "2/3") or (fec == 3 and "3/4") or \
			(fec == 4 and "5/6") or (fec == 5 and "7/8") or (fec == 6 and "8/9") or (fec == 7 and "3/5") or \
			(fec == 8 and "4/5") or (fec == 9 and "9/10") or (fec == 15 and "None") or "Unknown"

	def updateTranspondersList(self, orbpos, tr=None, pol=None):
		if orbpos is not None:
			index = 0
			default = "0"
			list = []
			tps = nimmanager.getTransponders(orbpos)
			for x in tps:
				if x[0] == 0:	#SAT
					s = str(x[1] // 1000) + " " + self.PolToStr(x[3]) + " / " + str(x[2] // 1000) + " / " + self.FecToStr(x[4])
					list.append((str(index), s))
					if tr is not None and tr == x[1] // 1000 and pol is not None and pol == x[3]:
						default = str(index)
					index += 1
			if orbpos == 360 and len(list) >= 20 and default == "0":
				default = "20"
			self.scan_transponders = ConfigSelection(choices=list, default=default)
			self.scan_transponders.addNotifier(self.retune, initial_call=False)

	def updateSatList(self):
		self.satList = []
		for slot in nimmanager.nim_slots:
			if slot.isCompatible("DVB-S"):
				self.satList.append(nimmanager.getSatListForNim(slot.slot))
			else:
				self.satList.append(None)

	def createSetup(self, firstStart=None):
		self.tuneTimer.stop()
		self.list = []
		self.multiscanlist = []
		if self.scan_nims == [] or self.scan_nims.value == "":
			return
		index_to_scan = int(self.scan_nims.value)
		config_list = True
		self.tunerEntry = getConfigListEntry(_("Tuner"), self.scan_nims)
		self.list.append(self.tunerEntry)
		self.typeOfScanEntry = None
		self.systemEntry = None
		self.satelliteEntry = None
		self.modulationEntry = None
		self.transpondersEntry = None
		self.scan_networkScan.value = False
		nim = nimmanager.nim_slots[index_to_scan]
		if nim.isCompatible("DVB-S"):
			self.typeOfScanEntry = getConfigListEntry(_("Type of scan"), self.scan_type)
			self.list.append(self.typeOfScanEntry)
			if self.scan_type.value == "single_transponder":
				self.updateSatList()
				sat = self.satList[index_to_scan][self.scan_satselection[index_to_scan].index]
				self.updateTranspondersList(sat[0])
				if nim.isCompatible("DVB-S2"):
					self.systemEntry = getConfigListEntry(_('System'), self.scan_sat.system)
					self.list.append(self.systemEntry)
				else:
					self.scan_sat.system.value = eDVBFrontendParametersSatellite.System_DVB_S
				self.list.append(getConfigListEntry(_('Satellite'), self.scan_satselection[index_to_scan]))
				self.list.append(getConfigListEntry(_('Frequency'), self.scan_sat.frequency))
				self.list.append(getConfigListEntry(_('Inversion'), self.scan_sat.inversion))
				self.list.append(getConfigListEntry(_('Symbol Rate'), self.scan_sat.symbolrate))
				self.list.append(getConfigListEntry(_("Polarity"), self.scan_sat.polarization))
				if self.scan_sat.system.value == eDVBFrontendParametersSatellite.System_DVB_S:
					self.list.append(getConfigListEntry(_("FEC"), self.scan_sat.fec))
				elif self.scan_sat.system.value == eDVBFrontendParametersSatellite.System_DVB_S2:
					self.list.append(getConfigListEntry(_("FEC"), self.scan_sat.fec_s2))
					self.modulationEntry = getConfigListEntry(_('Modulation'), self.scan_sat.modulation)
					self.list.append(self.modulationEntry)
					self.list.append(getConfigListEntry(_('Rolloff'), self.scan_sat.rolloff))
					self.list.append(getConfigListEntry(_('Pilot'), self.scan_sat.pilot))
			elif self.scan_type.value == "predefined_transponder":
				self.updateSatList()
				self.satelliteEntry = getConfigListEntry(_('Satellite'), self.scan_satselection[index_to_scan])
				self.list.append(self.satelliteEntry)
				sat = self.satList[index_to_scan][self.scan_satselection[index_to_scan].index]
				if firstStart is not None:
					self.updateTranspondersList(sat[0], tr=self.scan_sat.frequency.value, pol=self.scan_sat.polarization.value)
				else:
					self.updateTranspondersList(sat[0])
				self.transpondersEntry = getConfigListEntry(_('Transponder'), self.scan_transponders)
				self.list.append(self.transpondersEntry)
			elif self.scan_type.value == "single_satellite":
				self.updateSatList()
				sat = self.satList[index_to_scan][self.scan_satselection[index_to_scan].index]
				self.updateTranspondersList(sat[0])
				self.list.append(getConfigListEntry(_("Satellite"), self.scan_satselection[index_to_scan]))
				self.scan_networkScan.value = True
			elif "multisat" in self.scan_type.value:
				tlist = []
				SatList = nimmanager.getSatListForNim(index_to_scan)
				for x in SatList:
					if self.Satexists(tlist, x[0]) == 0:
						tlist.append(x[0])
						sat = ConfigEnableDisable(default=self.scan_type.value.find("_yes") != -1 and True or False)
						configEntry = getConfigListEntry(nimmanager.getSatDescription(x[0]), sat)
						self.list.append(configEntry)
						self.multiscanlist.append((x[0], sat))
						sat.addNotifier(self.retune, initial_call=False)
				self.scan_networkScan.value = True
			elif self.scan_type.value == "provider":
				config_list = False
				satList = nimmanager.getSatListForNim(index_to_scan)
				satchoises = [("none", _("None"))]
				for sat in satList:
					if sat[0] == 48 or sat[0] == 49:
						satchoises.append(("viasat", _("Viasat")))
						satchoises.append(("viasat_lat", _("Viasat Latvija")))
					elif sat[0] == 3560:
						satchoises.append(("viasat_ukr", _("Viasat Ukraine") + " (Amos)"))
					elif sat[0] == 90:
						satchoises.append(("xtratv", _("Xtra TV")))
					elif sat[0] == 360:
						satchoises.append(("ntv", _("NTV Plus")))
						satchoises.append(("tricolor", _("Tricolor TV")))
					elif sat[0] == 560:
						satchoises.append(("ntv_vostok", _("NTV Plus Vostok")))
						satchoises.append(("tricolor_sibir", _("Tricolor TV Sibir")))
					elif sat[0] == 585:
						satchoises.append(("otautv", _("Otau TV")))
					elif sat[0] == 750:
						#satchoises.append(("raduga", _("Raduga TV")))
						satchoises.append(("mtstv", _("MTS TV")))
					elif sat[0] in (849, 850, 851):
						satchoises.append(("kontinent", _("Telekarta (HD)")))
					elif sat[0] == 1400:
						satchoises.append(("ntv_dalvostok", _("NTV Plus Dalniy Vostok")))
						satchoises.append(("kontinent_dalvostok", _("Telekarta-Vostok")))
				self.provider_list = ConfigSelection(default="none", choices=satchoises)
				ProviderEntry = getConfigListEntry(_("Provider"), self.provider_list)
				self.list.append(ProviderEntry)
				self.provider_list.addNotifier(self.retune, initial_call=False)
				if self.provider_list.value == "none":
					self.retune(None)
		self.list.append(getConfigListEntry(_("Network scan"), self.scan_networkScan))
		clear_text = _("Clear before scan")
		if self.scan_type.value == "predefined_transponder" or self.scan_type.value == "single_transponder":
			clear_text += _(" (only this transponder)")
		elif self.scan_type.value == "provider":
			clear_text += _(" (only transponders provider)")
		self.list.append(getConfigListEntry(clear_text, self.scan_clearallservices))
		if config_list:
			self.list.append(getConfigListEntry(_("Only free scan"), self.scan_onlyfree))
		self["config"].list = self.list
		self["config"].l.setList(self.list)

	def Satexists(self, tlist, pos):
		for x in tlist:
			if x == pos:
				return 1
		return 0

	def newConfig(self):
		cur = self["config"].getCurrent()
		if cur == self.tunerEntry:
			if not self.scan_nims.value == "":
				self.feid = int(self.scan_nims.value)
				self.deInitFrontend()
				self.initFrontend()
				self.createSetup()
		elif cur == self.typeOfScanEntry or \
			cur == self.systemEntry or \
			cur == self.satelliteEntry or \
			(self.modulationEntry and self.systemEntry[1].value == eDVBFrontendParametersSatellite.System_DVB_S2 and cur == self.modulationEntry):
			self.createSetup()

	def getNimvalue(self):
		nim_list = []
		for n in nimmanager.nim_slots:
			if hasattr(n, 'config_mode'):
				if n.config_mode == "nothing":
					continue
				if hasattr(n, "isFBCLink") and n.isFBCLink():
					continue
				if n.config_mode in ("simple", "equal", "advanced") and len(nimmanager.getSatListForNim(n.slot)) < 1:
					continue
				if n.config_mode in ("loopthrough", "satposdepends"):
					root_id = nimmanager.sec.getRoot(n.slot_id, int(n.config.connectedTo.value))
					if n.type == nimmanager.nim_slots[root_id].type:
						continue
				if n.isCompatible("DVB-S"):
					nim_list.append((str(n.slot), n.friendly_full_description))
			elif hasattr(n, 'config') and hasattr(n.config, 'dvbs') and hasattr(n.config.dvbs, 'configMode'):
				if n.config.dvbs.configMode.value == "nothing":
					continue
				if hasattr(n, "isFBCLink") and n.isFBCLink():
					continue
				if n.config.dvbs.configMode.value in ("simple", "equal", "advanced") and len(nimmanager.getSatListForNim(n.slot)) < 1:
					continue
				if n.config.dvbs.configMode.value in ("loopthrough", "satposdepends"):
					root_id = nimmanager.sec.getRoot(n.slot_id, int(n.config.dvbs.connectedTo.value))
					if n.type == nimmanager.nim_slots[root_id].type:
						continue
				if n.isCompatible("DVB-S"):
					nim_list.append((str(n.slot), n.friendly_full_description))
		self.scan_nims = ConfigSelection(choices=nim_list)
		return nim_list

	def createConfig(self, frontendData):
		defaultSat = {
			"orbpos": 192,
			"system": eDVBFrontendParametersSatellite.System_DVB_S,
			"frequency": 11836,
			"inversion": eDVBFrontendParametersSatellite.Inversion_Unknown,
			"symbolrate": 27500,
			"polarization": eDVBFrontendParametersSatellite.Polarisation_Horizontal,
			"fec": eDVBFrontendParametersSatellite.FEC_Auto,
			"fec_s2": eDVBFrontendParametersSatellite.FEC_9_10,
			"modulation": eDVBFrontendParametersSatellite.Modulation_QPSK}

		default_scan = "single_transponder"
		if frontendData is not None:
			ttype = frontendData.get("tuner_type", "UNKNOWN")
			if ttype == "DVB-S":
				defaultSat["system"] = frontendData.get("system", eDVBFrontendParametersSatellite.System_DVB_S)
				defaultSat["frequency"] = frontendData.get("frequency", 0) // 1000
				defaultSat["inversion"] = frontendData.get("inversion", eDVBFrontendParametersSatellite.Inversion_Unknown)
				defaultSat["symbolrate"] = frontendData.get("symbol_rate", 0) // 1000
				defaultSat["polarization"] = frontendData.get("polarization", eDVBFrontendParametersSatellite.Polarisation_Horizontal)
				if defaultSat["system"] == eDVBFrontendParametersSatellite.System_DVB_S2:
					defaultSat["fec_s2"] = frontendData.get("fec_inner", eDVBFrontendParametersSatellite.FEC_Auto)
					defaultSat["rolloff"] = frontendData.get("rolloff", eDVBFrontendParametersSatellite.RollOff_alpha_0_35)
					defaultSat["pilot"] = frontendData.get("pilot", eDVBFrontendParametersSatellite.Pilot_Unknown)
				else:
					defaultSat["fec"] = frontendData.get("fec_inner", eDVBFrontendParametersSatellite.FEC_Auto)
				defaultSat["modulation"] = frontendData.get("modulation", eDVBFrontendParametersSatellite.Modulation_QPSK)
				if 'orbital_position' in frontendData:
					defaultSat["orbpos"] = frontendData['orbital_position']
				default_scan = "predefined_transponder"

		self.scan_sat = ConfigSubsection()
		scan_choices = {
		"single_transponder": _("User defined transponder"),
		"predefined_transponder": _("Predefined transponder"),
		"single_satellite": _("Single satellite"),
		"multisat": _("Multisat"),
		"multisat_yes": _("Multisat all select")}
		if self.providersSat():
			scan_choices = {
			"single_transponder": _("User defined transponder"),
			"predefined_transponder": _("Predefined transponder"),
			"single_satellite": _("Single satellite"),
			"multisat": _("Multisat"),
			"multisat_yes": _("Multisat all select"),
			"provider": _("Russian providers")}
		self.scan_type = ConfigSelection(choices=scan_choices, default=default_scan)
		self.scan_transponders = None
		self.provider_list = None
		self.scan_clearallservices = ConfigSelection(default="no", choices=[("no", _("no")), ("yes", _("yes")), ("yes_hold_feeds", _("yes (keep feeds)"))])
		self.scan_onlyfree = ConfigYesNo(default=False)
		self.scan_networkScan = ConfigYesNo(default=False)

		self.scan_sat.system = ConfigSelection(default=defaultSat["system"], choices=[
			(eDVBFrontendParametersSatellite.System_DVB_S, _("DVB-S")),
			(eDVBFrontendParametersSatellite.System_DVB_S2, _("DVB-S2"))])
		self.scan_sat.frequency = ConfigInteger(default=defaultSat["frequency"], limits=(1, 99999))
		self.scan_sat.inversion = ConfigSelection(default=defaultSat["inversion"], choices=[
			(eDVBFrontendParametersSatellite.Inversion_Off, _("Off")),
			(eDVBFrontendParametersSatellite.Inversion_On, _("On")),
			(eDVBFrontendParametersSatellite.Inversion_Unknown, _("Auto"))])
		self.scan_sat.symbolrate = ConfigInteger(default=defaultSat["symbolrate"], limits=(1, 99999))
		self.scan_sat.polarization = ConfigSelection(default=defaultSat["polarization"], choices=[
			(eDVBFrontendParametersSatellite.Polarisation_Horizontal, _("horizontal")),
			(eDVBFrontendParametersSatellite.Polarisation_Vertical, _("vertical")),
			(eDVBFrontendParametersSatellite.Polarisation_CircularLeft, _("circular left")),
			(eDVBFrontendParametersSatellite.Polarisation_CircularRight, _("circular right"))])
		self.scan_sat.fec = ConfigSelection(default=defaultSat["fec"], choices=[
			(eDVBFrontendParametersSatellite.FEC_Auto, _("Auto")),
			(eDVBFrontendParametersSatellite.FEC_1_2, "1/2"),
			(eDVBFrontendParametersSatellite.FEC_2_3, "2/3"),
			(eDVBFrontendParametersSatellite.FEC_3_4, "3/4"),
			(eDVBFrontendParametersSatellite.FEC_5_6, "5/6"),
			(eDVBFrontendParametersSatellite.FEC_7_8, "7/8"),
			(eDVBFrontendParametersSatellite.FEC_None, _("None"))])
		self.scan_sat.fec_s2 = ConfigSelection(default=defaultSat["fec_s2"], choices=[
			(eDVBFrontendParametersSatellite.FEC_Auto, _("Auto")),
			(eDVBFrontendParametersSatellite.FEC_1_2, "1/2"),
			(eDVBFrontendParametersSatellite.FEC_2_3, "2/3"),
			(eDVBFrontendParametersSatellite.FEC_3_4, "3/4"),
			(eDVBFrontendParametersSatellite.FEC_3_5, "3/5"),
			(eDVBFrontendParametersSatellite.FEC_4_5, "4/5"),
			(eDVBFrontendParametersSatellite.FEC_5_6, "5/6"),
			(eDVBFrontendParametersSatellite.FEC_7_8, "7/8"),
			(eDVBFrontendParametersSatellite.FEC_8_9, "8/9"),
			(eDVBFrontendParametersSatellite.FEC_9_10, "9/10")])
		lst = [(eDVBFrontendParametersSatellite.Modulation_QPSK, "QPSK"), (eDVBFrontendParametersSatellite.Modulation_8PSK, "8PSK")]
		if hasattr(eDVBFrontendParametersSatellite, "Modulation_16APSK") and hasattr(eDVBFrontendParametersSatellite, "Modulation_32APSK"):
			lst += [(eDVBFrontendParametersSatellite.Modulation_16APSK, "16APSK"), (eDVBFrontendParametersSatellite.Modulation_32APSK, "32APSK")]
		self.scan_sat.modulation = ConfigSelection(default=defaultSat["modulation"], choices=lst)
		lst = [(eDVBFrontendParametersSatellite.RollOff_alpha_0_35, "0.35"), (eDVBFrontendParametersSatellite.RollOff_alpha_0_25, "0.25"), (eDVBFrontendParametersSatellite.RollOff_alpha_0_20, "0.20")]
		if hasattr(eDVBFrontendParametersSatellite, "RollOff_auto"):
			lst += [(eDVBFrontendParametersSatellite.RollOff_auto, _("Auto"))]
		self.scan_sat.rolloff = ConfigSelection(default=defaultSat.get("rolloff", eDVBFrontendParametersSatellite.RollOff_alpha_0_35), choices=lst)
		self.scan_sat.pilot = ConfigSelection(default=defaultSat.get("pilot", eDVBFrontendParametersSatellite.Pilot_Unknown), choices=[
			(eDVBFrontendParametersSatellite.Pilot_Off, _("Off")),
			(eDVBFrontendParametersSatellite.Pilot_On, _("On")),
			(eDVBFrontendParametersSatellite.Pilot_Unknown, _("Auto"))])

		self.scan_scansat = {}
		for sat in nimmanager.satList:
			self.scan_scansat[sat[0]] = ConfigYesNo(default=False)

		self.scan_satselection = []
		for slot in nimmanager.nim_slots:
			if slot.isCompatible("DVB-S"):
				x = getConfigSatlist(defaultSat["orbpos"], self.satList[slot.slot])
				x.addNotifier(self.retune, initial_call=False)
				self.scan_satselection.append(x)
			else:
				self.scan_satselection.append(None)

		for x in (self.scan_nims, self.scan_type, self.scan_sat.frequency,
			self.scan_sat.inversion, self.scan_sat.symbolrate,
			self.scan_sat.polarization, self.scan_sat.fec, self.scan_sat.pilot,
			self.scan_sat.fec_s2, self.scan_sat.fec, self.scan_sat.modulation,
			self.scan_sat.rolloff, self.scan_sat.system):
			x.addNotifier(self.retune, initial_call=False)

	def keyLeft(self):
		ConfigListScreen.keyLeft(self)
		self.newConfig()

	def keyRight(self):
		ConfigListScreen.keyRight(self)
		self.newConfig()

	def addSatTransponder(self, tlist, frequency, symbol_rate, polarisation, fec, inversion, orbital_position, system, modulation, rolloff, pilot):
		#print("Add Sat: frequency: " + str(frequency) + " symbol: " + str(symbol_rate) + " pol: " + str(polarisation) + " fec: " + str(fec) + " inversion: " + str(inversion) + " modulation: " + str(modulation) + " system: " + str(system) + " rolloff" + str(rolloff) + " pilot" + str(pilot))
		#print("orbpos: " + str(orbital_position))
		parm = eDVBFrontendParametersSatellite()
		parm.modulation = modulation
		parm.system = system
		parm.frequency = frequency * 1000
		parm.symbol_rate = symbol_rate * 1000
		parm.polarisation = polarisation
		parm.fec = fec
		parm.inversion = inversion
		parm.orbital_position = orbital_position
		parm.rolloff = rolloff
		parm.pilot = pilot
		tlist.append(parm)

	def getInitialTransponderList(self, tlist, pos):
		list = nimmanager.getTransponders(pos)
		for x in list:
			if x[0] == 0:
				parm = eDVBFrontendParametersSatellite()
				parm.frequency = x[1]
				parm.symbol_rate = x[2]
				parm.polarisation = x[3]
				parm.fec = x[4]
				parm.inversion = x[7]
				parm.orbital_position = pos
				parm.system = x[5]
				parm.modulation = x[6]
				parm.rolloff = x[8]
				parm.pilot = x[9]
				tlist.append(parm)

	def getInitialTransponderProviderList(self, tlist, pos, providers=None):
		list = nimmanager.getTransponders(pos)
		for x in list:
			pol = x[3]
			if x[3] == 2:
				pol = 0
			if x[3] == 3:
				pol = 1
			if x[0] == 0 and providers is not None and (x[1], pol) in providers:
				fec = x[4]
				system = x[5]
				if providers is TRIKOLOR and x[1] == 12360000 and (x[3] == 3 or x[3] == 1):
					fec = 0
					system = 1
				elif providers is KONTINENT and x[1] == 11960000 and x[3] == 0:
					fec = 2
				parm = eDVBFrontendParametersSatellite()
				parm.frequency = x[1]
				parm.symbol_rate = x[2]
				parm.polarisation = x[3]
				parm.fec = fec
				parm.inversion = x[7]
				parm.orbital_position = pos
				parm.system = system
				parm.modulation = x[6]
				parm.rolloff = x[8]
				parm.pilot = x[9]
				tlist.append(parm)

	def providersSat(self):
		providers_sat = False
		for sat in nimmanager.satList:
			if sat[0] == 48 or sat[0] == 49 or sat[0] == 90 or sat[0] == 360 or sat[0] == 560 or sat[0] == 585 or sat[0] == 750 or sat[0] in (849, 850, 851) or sat[0] == 1400 or sat[0] == 3560:
				providers_sat = True
				break
		return providers_sat

	def keyOK(self):
		try:
			cur = self["config"].getCurrent()
			if self.scan_type.value == "predefined_transponder" and self.transpondersEntry is not None and cur == self.transpondersEntry:
				index_to_scan = int(self.scan_nims.value)
				sat = self.satList[index_to_scan][self.scan_satselection[index_to_scan].index]
				if sat[0] is not None:
					tr_list = self.createTranspondersList(sat[0])
					if len(tr_list) > 1:
						orbos = self.OrbToStr(sat[0])
						self.session.openWithCallback(self.configCallback, TranspondersList, list=tr_list, sat=orbos)
			else:
				self.keyGo()
		except:
			pass

	def configCallback(self, callback=None):
		try:
			if callback is not None:
				self.scan_transponders.value = callback
		except:
			pass

	def createTranspondersList(self, orbpos):
		list = []
		index = 0
		tps = nimmanager.getTransponders(orbpos)
		for x in tps:
			if x[0] == 0:
				s = str(x[1] // 1000) + " " + self.PolToStr(x[3]) + " / " + str(x[2] // 1000) + " / " + self.FecToStr(x[4])
				list.append((s, str(index)))
				index += 1
		return list

	def keyGo(self):
		if self.frontend is None:
			text = _("Sorry, this tuner is in use.")
			if self.session.nav.getRecordings():
				text += "\n"
				text += _("Maybe the reason that recording is currently running.")
			self.session.open(MessageBox, text, MessageBox.TYPE_ERROR)
			return
		if self.scan_nims.value == "":
			return
		self.tuneTimer.stop()
		self.deInitFrontend()
		index_to_scan = int(self.scan_nims.value)
		self.feid = index_to_scan
		tlist = []
		flags = None
		startScan = True
		removeAll = True
		if self.scan_nims == []:
			self.session.open(MessageBox, _("No tuner is enabled!\nPlease setup your tuner settings before you start a service scan."), MessageBox.TYPE_ERROR)
			return
		nim = nimmanager.nim_slots[index_to_scan]
		if not nim.isCompatible("DVB-S"):
			return
		if self.scan_type.value.find("_transponder") != -1:
			assert len(self.satList) > index_to_scan
			assert len(self.scan_satselection) > index_to_scan
			nimsats = self.satList[index_to_scan]
			selsatidx = self.scan_satselection[index_to_scan].index
			if len(nimsats):
				orbpos = nimsats[selsatidx][0]
				if self.scan_type.value == "single_transponder":
					if self.scan_sat.system.value == eDVBFrontendParametersSatellite.System_DVB_S:
						fec = self.scan_sat.fec.value
					else:
						fec = self.scan_sat.fec_s2.value
					self.addSatTransponder(tlist, self.scan_sat.frequency.value,
								self.scan_sat.symbolrate.value,
								self.scan_sat.polarization.value,
								fec,
								self.scan_sat.inversion.value,
								orbpos,
								self.scan_sat.system.value,
								self.scan_sat.modulation.value,
								self.scan_sat.rolloff.value,
								self.scan_sat.pilot.value)
				elif self.scan_type.value == "predefined_transponder":
					tps = nimmanager.getTransponders(orbpos)
					if len(tps) > self.scan_transponders.index:
						x = tps[self.scan_transponders.index]
						self.addSatTransponder(tlist, x[1] // 1000, x[2] // 1000, x[3], x[4], x[7], orbpos, x[5], x[6], x[8], x[9])
			removeAll = False
		elif self.scan_type.value == "single_satellite":
			sat = self.satList[index_to_scan][self.scan_satselection[index_to_scan].index]
			self.getInitialTransponderList(tlist, sat[0])
		elif self.scan_type.value.find("multisat") != -1:
			SatList = nimmanager.getSatListForNim(index_to_scan)
			for x in self.multiscanlist:
				if x[1].value:
					self.getInitialTransponderList(tlist, x[0])
		elif self.scan_type.value == "provider":
			if self.provider_list is not None:
				if self.provider_list.value == "none":
					startScan = False
				else:
					orbpos = 0
					startScan = False
					providerList = None
					viasat = False
					kontinent = False
					if self.provider_list.value == "viasat":
						orbpos = 49
						providerList = VIASAT
						viasat = True
					elif self.provider_list.value == "viasat_lat":
						orbpos = 49
						providerList = VIASATLATVIJA
						viasat = True
					elif self.provider_list.value == "xtratv":
						orbpos = 90
						providerList = XTRATV
					elif self.provider_list.value == "viasat_ukr":
						orbpos = 3560
						providerList = VIASATUKR
					elif self.provider_list.value == "ntv":
						orbpos = 360
						providerList = NTVPLUS
					elif self.provider_list.value == "tricolor":
						orbpos = 360
						providerList = TRIKOLOR
					elif self.provider_list.value == "ntv_vostok":
						orbpos = 560
						providerList = NTVPLUS_VOSTOK
					elif self.provider_list.value == "tricolor_sibir":
						orbpos = 560
						providerList = TRIKOLOR_SIBIR
					elif self.provider_list.value == "otautv":
						orbpos = 585
						providerList = OTAUTV
					elif self.provider_list.value == "raduga":
						orbpos = 750
						providerList = RADUGA
					elif self.provider_list.value == "mtstv":
						orbpos = 750
						providerList = MTSTV
					elif self.provider_list.value == "kontinent":
						orbpos = 850
						providerList = KONTINENT
						kontinent = True
					elif self.provider_list.value == "ntv_dalvostok":
						orbpos = 1400
						providerList = NTVPLUS_DAL_VOSTOK
					elif self.provider_list.value == "kontinent_dalvostok":
						orbpos = 1400
						providerList = KONTINENT_DAL_VOSTOK
					if orbpos > 0 and providerList is not None:
						SatList = nimmanager.getSatListForNim(index_to_scan)
						for sat in SatList:
							if sat[0] == orbpos or (viasat and sat[0] in (48, 49)) or (kontinent and sat[0] in (849, 850, 851)):
								self.getInitialTransponderProviderList(tlist, sat[0], providers=providerList)
						removeAll = False
						startScan = True

		flags = self.scan_networkScan.value and eComponentScan.scanNetworkSearch or 0
		tmp = self.scan_clearallservices.value
		self.kept_feed_snapshot = None
		if tmp == "yes":
			flags |= eComponentScan.scanRemoveServices
		elif tmp == "yes_hold_feeds":
			current_orb = None
			try:
				# Check which configuration variable holds the satellite position
				if hasattr(self, 'scan_sat') and hasattr(self.scan_sat, 'value'):
					current_orb = int(self.scan_sat.value)
				elif hasattr(self, 'satList') and hasattr(self.satList, 'value'):
					current_orb = int(self.satList.value)
			except:
				pass

			# Pass the current satellite position to only snapshot feeds on THIS sat
			self.kept_feed_snapshot = collectKeepServicesSnapshot(orb_filter=current_orb)
			flags |= eComponentScan.scanRemoveServices

		if tmp == "yes" and not removeAll:
			flags |= eComponentScan.scanDontRemoveUnscanned

		if self.scan_onlyfree.value:
			flags |= eComponentScan.scanOnlyFree

		for x in self["config"].list:
			x[1].save()

		if startScan:
			self.startScan(tlist, flags, index_to_scan)

	def keyCancel(self):
		if self.session.postScanService and self.stop_service:
			self.session.openWithCallback(self.restartPrevService, MessageBox, _("Zap back to service before a service scan?"), MessageBox.TYPE_YESNO, timeout=10)
		else:
			self.restartPrevService(True)

	def restartPrevService(self, answer):
		if self.scan_clearallservices.value == "yes_hold_feeds":
			appendSnapshotServicesToLamedb(getattr(self, "kept_feed_snapshot", None))
			self.kept_feed_snapshot = None
		for x in self["config"].list:
			x[1].cancel()
		self.tuneTimer.stop()
		self.deInitFrontend()
		if answer:
			self.session.nav.playService(self.session.postScanService)
		self.close()

	def handleKeyFileCallback(self, answer):
		ConfigListScreen.handleKeyFileCallback(self, answer)
		self.newConfig()

	def startScan(self, tlist, flags, feid):
		if len(tlist):
			self.session.openWithCallback(self.serviceScanFinished, ServiceScan, [{"transponders": tlist, "feid": feid, "flags": flags}])
		else:
			self.session.open(MessageBox, _("Nothing to scan!\nPlease setup your tuner settings before you start a service scan."), MessageBox.TYPE_ERROR)
			self.keyCancel()

	def serviceScanFinished(self, answer=None):
		if answer is not True:
			self.session.openWithCallback(self.restartSignalFinder, MessageBox, _("Do you want to scan another transponder/satellite?"), MessageBox.TYPE_YESNO, timeout=10)
		elif answer is True:
			self.restartPrevService(True)

	def restartSignalFinder(self, answer):
		if answer:
			self.tuneTimer.stop()
			self.deInitFrontend()
			self.initFrontend()
		else:
			self.keyCancel()


def SignalFinderMain(session, **kwargs):
	nims = nimmanager.nim_slots
	sat_nimList = []
	for x in nims:
		if not x.isCompatible("DVB-S"):
			continue
		try:
			nimConfig = nimmanager.getNimConfig(x.slot).dvbs
		except:
			nimConfig = nimmanager.getNimConfig(x.slot)
		if not hasattr(nimConfig, 'configMode'):
			continue
		if nimConfig.configMode.value in ("loopthrough", "satposdepends", "nothing"):
			continue
		try:
			if nimConfig.configMode.value == "advanced" and int(nimConfig.advanced.sat[3607].lnb.value) != 0:
				continue
		except:
			pass
		if nimConfig.configMode.value in ("simple", "advanced") and len(nimmanager.getSatListForNim(x.slot)) < 1:
			nimConfig.configMode.value = "nothing"
			nimConfig.configMode.save()
			continue
		sat_nimList.append(x)
	sat = len(sat_nimList)
	record = session.nav.getRecordings()
	if sat == 0:
		session.open(MessageBox, _("No satellites configured for DVB-S2 tuner(s).\nPlease check your tuner(s) setup."), MessageBox.TYPE_ERROR)
		return
	if record and sat == 1:
		session.open(MessageBox, _("A recording is currently running. Please stop the recording before trying to start a service scan."), MessageBox.TYPE_ERROR)
		return
	if t2mi and multistream:
		session.open(SignalFinderMultistreamT2MI)
	elif multistream:
		session.open(SignalFinderMultistream)
	else:
		session.open(SignalFinder)


def SignalFinderStart(menuid, **kwargs):
	if menuid == "scan":
		return [(_("Signal Finder DVB-S2"), SignalFinderMain, "signal_finder", None)]
	else:
		return []


def Plugins(**kwargs):
	if nimmanager.hasNimType("DVB-S"):
		return PluginDescriptor(name="Signal Finder DVB-S2", description=_("Signal finder for DVB-S2 tuners"), where=PluginDescriptor.WHERE_MENU, fnc=SignalFinderStart)
	else:
		return []
