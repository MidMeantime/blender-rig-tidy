# Rig Tidy core module. The add-on entry point is __init__.py in this folder.
bl_info = {
    "name": "Rig Tidy",
    "author": "MidMeantime",
    "version": (0, 8, 1),
    "blender": (4, 2, 0),
    "location": "View3D > Sidebar > Rig Tidy",
    "description": "Cleans up imported Biped / game rigs: bone directions & rolls, "
                   "swapped L/R names, and Blender-style renaming (vertex groups follow)",
    "category": "Rigging",
}

"""
Rig Tidy
--------
Built from cleaning up three FBX imports from the same game (3ds Max Biped body,
"BN_*_SkinJNT" face, numbered "Bone_##" secondary chains, optional props).

Workflow (View3D > Sidebar > Rig Tidy, with the armature active):
  1. Analyze        - recognises Biped body + face bones, finds every other chain,
                      pairs mirrored chains, guesses a category, checks L/R names
                      against where the weights really are.
  2. Assign chains  - pick Hair / Skirt / Cape / Prop / ... for each unnamed chain
                      (mirror partners follow).  "Select" and "Test" show what a
                      chain moves.
  3. Apply          - backup, fix swapped names, re-point every bone at its child,
                      consistent rolls, drop import pose noise, rename.
                      Bone heads (pivots) never move, so skinning is untouched.
  4. Test Pose      - toggles a symmetric pose to check bends, toggle again to reset.
"""

import bpy
import re
import os
import math
from mathutils import Vector, Matrix
from bpy.props import (StringProperty, EnumProperty, BoolProperty, IntProperty,
                       FloatProperty, CollectionProperty, PointerProperty)

# ---------------------------------------------------------------------------
# Name recognition
# ---------------------------------------------------------------------------

FINGERS = ['thumb', 'index', 'middle', 'ring', 'pinky']
LIMB_PART = {'upperarm': 'upper_arm', 'uparm': 'upper_arm', 'forearm': 'forearm',
             'fore': 'forearm', 'thigh': 'thigh', 'calf': 'shin'}
LIMB_CHILD = {'upper_arm': 'forearm', 'forearm': 'hand', 'thigh': 'shin', 'shin': 'foot'}
BIPED_SIMPLE = {'Clavicle': 'clavicle', 'UpperArm': 'upper_arm', 'Forearm': 'forearm',
                'Hand': 'hand', 'Thigh': 'thigh', 'Calf': 'shin', 'Foot': 'foot',
                'HorseLink': 'horselink'}
HELPERS = {'xigai': 'knee', 'shouzhou': 'elbow'}


def parse_body(name):
    """Biped body bone -> (role, side, idx) or None."""
    if re.fullmatch(r'(?i)root(bone)?', name):
        return ('root', None, 0)
    if re.fullmatch(r'Bip\d+', name):
        return ('com', None, 0)
    m = re.fullmatch(r'Bip\d+(?: ([LR]))? (.+)', name)
    if m:
        side, part = m.group(1), m.group(2)
        if part == 'Pelvis':
            return ('pelvis', None, 0)
        mm = re.fullmatch(r'Spine(\d*)', part)
        if mm:
            return ('spine', None, int(mm.group(1) or 0))
        mm = re.fullmatch(r'Neck(\d*)', part)
        if mm:
            return ('neck', None, int(mm.group(1) or 0))
        if part == 'Head':
            return ('head', None, 0)
        if side:
            if part in BIPED_SIMPLE:
                return (BIPED_SIMPLE[part], side, 0)
            mm = re.fullmatch(r'Toe0(\d*)', part)
            if mm:
                return ('toe', side, int(mm.group(1) or 0))
            mm = re.fullmatch(r'Finger(\d)(\d*)', part)
            if mm:
                return ('finger', side, (int(mm.group(1)), int(mm.group(2) or 0)))
            mm = re.fullmatch(r'(?i)(upperarm|uparm|forearm|fore|thigh|calf)twist(\d*)', part)
            if mm:
                return ('twist', side, (LIMB_PART[mm.group(1).lower()], int(mm.group(2) or 0)))
        return None
    mm = re.fullmatch(r'(?i)bone[_ ]?([LR])[_ ]?(upperarm|uparm|forearm|fore|thigh|calf)[_ ]?twists?(\d*)', name)
    if mm:
        return ('twist', mm.group(1).upper(), (LIMB_PART[mm.group(2).lower()], int(mm.group(3) or 0)))
    mm = re.fullmatch(r'(?i)bone[_ ]?xiuxing[_ ]?(xigai|shouzhou)[_ ]?([lr])', name)
    if mm:
        return ('helper', mm.group(2).upper(), HELPERS[mm.group(1).lower()])
    return None


# (regex, new base name). (?P<s>) = side, (?P<n>) = number -> {n2} as two digits.
# First match wins, so specific rules go before general ones.
_S = r'(?P<s>Left|Right)'
FACE_RULES = [
    (r'Bn_Upper_Head', 'head_upper'),
    (r'Bn_Lower_Head', 'head_lower'),
    (r'BN_Jaw_SkinJNT', 'jaw'),
    (r'BN_shangya_M', 'teeth_upper'),
    (r'BN_xiaya_M', 'teeth_lower'),
    (r'BN_xiaba_C', 'chin'),
    (r'BN_xiaba_(?P<s>[LR])', 'chin'),
    (r'BN_bizi', 'nose'),
    (r'BN_bichungou_(?P<s>[LR])', 'nasolabial_01'),
    (r'BN_bichungou1_(?P<s>[LR])', 'nasolabial_02'),
    (r'BN_Mid_Brow_SkinJNT', 'brow_mid'),
    (r'BN_Mid_Lip_Up_SkinJNT', 'lip_upper'),
    (r'BN_Mid_Lip_Dn_SkinJNT', 'lip_lower'),
    (r'BN_shetou\d+', 'tongue'),                       # numbered root->tip later
    (rf'BN_{_S}_Cheek0_SkinJNT', 'cheek_outer'),
    (rf'BN_{_S}_Cheek3_SkinJNT', 'cheek_inner'),
    (rf'BN_{_S}_CheekRaiser\d*_SkinJNT', 'cheek_raiser'),
    (rf'BN_{_S}_Cheek(?P<n>\d+)_SkinJNT', 'cheek_{n2}'),
    (rf'BN_{_S}_Lip_Up(?P<n>\d+)_SkinJNT', 'lip_upper_{n2}'),
    (rf'BN_{_S}_Lip_Dn(?P<n>\d+)_SkinJNT', 'lip_lower_{n2}'),
    (rf'BN_{_S}_Brow(?P<n>\d+)_SkinJNT', 'brow_{n2}'),
    (rf'BN_{_S}_Eye_SkinJNT', 'eye'),
    (rf'BN_{_S}_Eyeball', 'eyeball'),
    (rf'BN_{_S}_UpLid\d*_SkinJNT', 'lid_upper_mid'),
    (rf'BN_{_S}_UpLid\d*_SkinJNT0*1', 'lid_upper_outer'),
    (rf'BN_{_S}_UpLid\d*_SkinJNT0*2', 'lid_upper_inner'),
    (rf'BN_{_S}_DnLid\d*_SkinJNT', 'lid_lower_mid'),
    (rf'BN_{_S}_DnLid\d*_SkinJNT0*1', 'lid_lower_outer'),
    (rf'BN_{_S}_DnLid\d*_SkinJNT0*2', 'lid_lower_inner'),
    (rf'BN_{_S}_CornerInLid_SkinJNT', 'lid_corner_inner'),
    (rf'BN_{_S}_CornerOutLid_SkinJNT', 'lid_corner_outer'),
    (r'Bone_(?P<s>[LR])star0*(?P<n>\d+)', 'eye_star_{n2}'),
    (r'Bone_(?P<s>[LR])_brow', 'brow_alt'),
    (r'Bone_(?P<s>[LR])_brow0*(?P<n>\d+)', 'brow_alt_{n2}'),
]
FACE_RULES = [(re.compile(p), t) for p, t in FACE_RULES]
SIDE_TOKEN = {'Left': 'L', 'Right': 'R', 'L': 'L', 'R': 'R'}


def parse_face(name):
    for rx, tmpl in FACE_RULES:
        m = rx.fullmatch(name)
        if m:
            gd = m.groupdict()
            side = SIDE_TOKEN.get(gd.get('s')) if gd.get('s') else None
            n2 = '%02d' % int(gd['n']) if gd.get('n') else ''
            return (tmpl.format(n2=n2), side, 0)
    return None


TIDY_SIDE = re.compile(r'(.+?)(?:[._]([LRlr]))?')
TIDY_BODY_SIMPLE = {'pelvis', 'head', 'clavicle', 'upper_arm', 'forearm', 'hand', 'thigh', 'shin',
                    'foot', 'horselink'}
TIDY_FACE = re.compile(
    r'head_upper|head_lower|jaw|teeth_upper|teeth_lower|chin|nose|nasolabial_\d\d|brow_mid|'
    r'lip_upper|lip_lower|lip_upper_\d\d|lip_lower_\d\d|cheek_outer|cheek_inner|cheek_raiser|'
    r'cheek_\d\d|brow_\d\d|eye|eyeball|lid_(upper|lower)_(mid|outer|inner)|lid_corner_(inner|outer)|'
    r'eye_star_\d\d|brow_alt|brow_alt_\d\d|tongue_\d\d')


def parse_tidy(name):
    """Names Rig Tidy itself produces -> (kind, role, side, idx) or None,
    so an already-cleaned rig can be analysed again."""
    m = TIDY_SIDE.fullmatch(name)
    base, side = m.group(1), (m.group(2).upper() if m.group(2) else None)
    if side is None:
        if base == 'root':
            return ('body', 'root', None, 0)
        if base == 'COM':
            return ('body', 'com', None, 0)
        if base in ('pelvis', 'head'):
            return ('body', base, None, 0)
        mm = re.fullmatch(r'spine_(\d\d)', base)
        if mm:
            return ('body', 'spine', None, int(mm.group(1)) - 1)
        mm = re.fullmatch(r'neck(?:_(\d\d))?', base)
        if mm:
            return ('body', 'neck', None, int(mm.group(1) or 1) - 1)
        if re.fullmatch(r'tongue_\d\d', base):
            return ('face', 'tongue', None, 0)
        if TIDY_FACE.fullmatch(base):
            return ('face', base, None, 0)
        return None
    if base in TIDY_BODY_SIMPLE:
        return ('body', base, side, 0)
    mm = re.fullmatch(r'toe(?:_(\d\d))?', base)
    if mm:
        return ('body', 'toe', side, int(mm.group(1) or 1) - 1)
    mm = re.fullmatch(r'(thumb|index|middle|ring|pinky)_(\d\d)', base)
    if mm:
        return ('body', 'finger', side, (FINGERS.index(mm.group(1)), int(mm.group(2)) - 1))
    mm = re.fullmatch(r'(upper_arm|forearm|thigh|shin)_twist(?:_(\d\d))?', base)
    if mm:
        return ('body', 'twist', side, (mm.group(1), int(mm.group(2) or 1) - 1))
    mm = re.fullmatch(r'(knee|elbow)_helper', base)
    if mm:
        return ('body', 'helper', side, mm.group(1))
    if TIDY_FACE.fullmatch(base):
        return ('face', base, side, 0)
    return None


GENERIC_NAME = re.compile(r'(?i)(bone|bn|b)[_ ]?\d+(\.\d+)?')

# ---------------------------------------------------------------------------
# Add-on preferences access (None when running outside an installed add-on)
# ---------------------------------------------------------------------------

_PREFS_OVERRIDE = None   # test hook


def get_prefs():
    if _PREFS_OVERRIDE is not None:
        return _PREFS_OVERRIDE
    try:
        return bpy.context.preferences.addons[__package__].preferences
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Name dictionary (dictionary.csv next to this file + optional user file)
# ---------------------------------------------------------------------------

_DICT = {'words': {}, 'sig': None, 'files': []}


def dictionary_files():
    files = [os.path.join(os.path.dirname(os.path.abspath(__file__)), 'dictionary.csv')]
    p = get_prefs()
    if p is not None and getattr(p, 'dictionary_path', ''):
        files.append(bpy.path.abspath(p.dictionary_path))
    return files


def load_dictionary(force=False):
    """{word: english} from every dictionary file (later files win). Cached by mtime."""
    files = [f for f in dictionary_files() if os.path.isfile(f)]
    try:
        sig = tuple((f, os.path.getmtime(f)) for f in files)
    except OSError:
        sig = None
    if not force and sig == _DICT['sig']:
        return _DICT['words']
    words = {}
    for f in files:
        try:
            with open(f, encoding='utf-8-sig') as fh:
                for line in fh:
                    line = line.strip()
                    if not line or line.startswith('#'):
                        continue
                    parts = [x.strip() for x in line.split(',', 1)]
                    if len(parts) < 2 or not parts[0] or not parts[1]:
                        continue
                    if parts[0].lower() == 'word':
                        continue  # header row
                    tr = re.sub(r'[^0-9a-zA-Z]+', '_', parts[1].lower()).strip('_')
                    if tr:
                        words[parts[0].lower()] = tr
        except OSError:
            pass
    _DICT.update(words=words, sig=sig, files=files)
    return words


def _translate_piece(piece, words):
    """Translate one separator-free piece, splitting camelCase if needed."""
    if piece.lower() in words:
        return words[piece.lower()]
    sub = re.sub(r'([a-z])([A-Z])', r'\1_\2', piece).split('_')
    if len(sub) > 1 and any(x.lower() in words for x in sub):
        return '_'.join(words.get(x.lower(), x) for x in sub)
    return piece


def translate_name(s, name):
    """Swap dictionary words inside a bone name; separators, numbers and sides stay."""
    if not getattr(s, 'opt_translate', False):
        return name
    words = load_dictionary()
    if not words:
        return name
    parts = re.split(r'([_.\- ]+)', name)
    return ''.join(p if (not p or re.fullmatch(r'[_.\- ]+', p)) else _translate_piece(p, words)
                   for p in parts)


FACE_WORDS = {
    'eye': 'eye', 'eyes': 'eye', 'eyeball': 'eyeball', 'eyelid': 'lid', 'lid': 'lid',
    'brow': 'brow', 'eyebrow': 'brow', 'jaw': 'jaw', 'chin': 'chin', 'nose': 'nose',
    'nostril': 'nose', 'cheek': 'cheek', 'mouth': 'mouth', 'lip': 'lip', 'lips': 'lip',
    'tongue': 'tongue', 'teeth': 'teeth', 'tooth': 'teeth',
    # pinyin used by this game's rigs
    'shetou': 'tongue', 'shangya': 'teeth_upper', 'xiaya': 'teeth_lower', 'xiaba': 'chin',
    'bizi': 'nose', 'yanjing': 'eye', 'meimao': 'brow', 'zuichun': 'lip', 'zui': 'mouth',
    'yanqiu': 'eyeball', 'yanpi': 'lid', 'zuijiao': 'mouth',
}
HAT_WORDS = {'hat', 'helmet', 'crown', 'cap', 'hood', 'tiara', 'headband', 'headwear', 'beret'}


def name_part(name):
    """('FACE'|'HAT', part) from typical words in a bone name, or None."""
    spaced = re.sub(r'([a-z])([A-Z])', r'\1_\2', name).lower()
    toks = [t for t in re.split(r'[^a-z]+', spaced) if t]
    words = load_dictionary()
    if words:   # dictionary words count too (e.g. a new pinyin word added by the user)
        toks = [x for t in toks for x in words.get(t, t).split('_') if x]
    for a, b in zip(toks, toks[1:]):          # two-word forms first: eye_lid, eye_brow
        if a + b in FACE_WORDS:
            return 'FACE', FACE_WORDS[a + b]
    for tok in toks:
        if tok in FACE_WORDS:
            return 'FACE', FACE_WORDS[tok]
        if tok in HAT_WORDS:
            return 'HAT', ''
    return None


PROP_MESH = re.compile(r'(?i)(weapon|wpn|prop|item|sword|staff|wand|umbrella|shield|gun|bow)')
HEAD_ROLES = {'head', 'head_upper', 'head_lower', 'jaw'}
ARM_ROLES = {'clavicle', 'upper_arm', 'forearm', 'hand', 'finger'}
LEG_ROLES = {'thigh', 'shin', 'foot', 'toe', 'horselink'}
TORSO_ROLES = {'root', 'com', 'pelvis', 'spine', 'neck'}


def classify(arm):
    info = {}
    for b in arm.data.bones:
        r = parse_body(b.name)
        if r:
            info[b.name] = {'kind': 'body', 'role': r[0], 'side': r[1], 'idx': r[2]}
            continue
        r = parse_face(b.name)
        if r:
            info[b.name] = {'kind': 'face', 'role': r[0], 'side': r[1], 'idx': r[2]}
            continue
        r = parse_tidy(b.name)
        if r:
            info[b.name] = {'kind': r[0], 'role': r[1], 'side': r[2], 'idx': r[3]}
            continue
        info[b.name] = {'kind': 'generic', 'role': None, 'side': None, 'idx': 0}
    # tongue bones: number by depth (root -> tip)
    tongue = [b for b in arm.data.bones if info[b.name]['role'] == 'tongue']
    tongue.sort(key=lambda b: len(b.parent_recursive))
    for k, b in enumerate(tongue):
        info[b.name]['idx'] = k
    return info


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------

def rig_axes(arm):
    """Armature-space (up, forward, character-left) unit vectors."""
    R = arm.matrix_world.to_3x3().normalized().inverted()
    up = (R @ Vector((0, 0, 1))).normalized()
    fwd = (R @ Vector((0, -1, 0))).normalized()
    left = (R @ Vector((1, 0, 0))).normalized()
    return up, fwd, left


def rig_scale(arm):
    hs = [b.head_local for b in arm.data.bones]
    if not hs:
        return 1.0
    lo = Vector((min(h.x for h in hs), min(h.y for h in hs), min(h.z for h in hs)))
    hi = Vector((max(h.x for h in hs), max(h.y for h in hs), max(h.z for h in hs)))
    return max((hi - lo).length, 1e-4)


def skinned_meshes(arm):
    return [o for o in bpy.data.objects if o.type == 'MESH' and
            any(m.type == 'ARMATURE' and m.object == arm for m in o.modifiers)]


def gather_weights(arm, min_w=0.01):
    """{group name: [(armature-space co, weight)]}, {group name: set(mesh names)}"""
    inv = arm.matrix_world.inverted()
    pts, owners = {}, {}
    for o in skinned_meshes(arm):
        M = inv @ o.matrix_world
        names = {g.index: g.name for g in o.vertex_groups}
        for v in o.data.vertices:
            co = None
            for g in v.groups:
                if g.weight < min_w:
                    continue
                n = names.get(g.group)
                if n is None:
                    continue
                if co is None:
                    co = M @ v.co
                pts.setdefault(n, []).append((co, g.weight))
                owners.setdefault(n, set()).add(o.name)
    return pts, owners


def weighted_centroid(pts, name):
    lst = pts.get(name)
    if not lst:
        return None
    sw = sum(w for c, w in lst)
    return sum((c * w for c, w in lst), Vector()) / sw


def mirror_point(p, left):
    return p - 2.0 * p.dot(left) * left


def suffix(s, side):
    if side not in ('L', 'R'):
        return ''
    return {'DOT': '.%s', 'UNDER': '_%s', 'DOTLOW': '.%s'}[s.suffix_style] % (
        side.lower() if s.suffix_style == 'DOTLOW' else side)


def clean_token(t):
    t = re.sub(r'[^0-9a-zA-Z]+', '_', t.strip().lower())
    return t.strip('_')


# ---------------------------------------------------------------------------
# Chains (everything not recognised)
# ---------------------------------------------------------------------------

def weight_axis(pts, name, head, min_w=0.25):
    """Principal axis of a bone's weighted verts, signed to point from the head toward
    the weights. Returns (axis, elongation ratio, centroid) or None."""
    lst = [c for c, w in pts.get(name, ()) if w > min_w]
    if len(lst) < 6:
        return None
    try:
        import numpy as np
    except ImportError:
        return None
    P = np.array([tuple(c) for c in lst])
    c = P.mean(axis=0)
    Q = P - c
    vals, vecs = np.linalg.eigh(Q.T @ Q / len(P))
    ax = Vector(vecs[:, 2]).normalized()
    ratio = float(vals[2] / max(vals[1], 1e-12))
    cen = Vector(c)
    if (cen - head).dot(ax) < 0:
        ax = -ax
    return ax, ratio, cen


ELONGATED = 3.0      # weight cloud this many times longer than wide counts as a clear axis
BRANCH_ALIGN = 35.0  # degrees: a child within this of the incoming direction continues the chain


def detect_chains(arm, info, pts=None, detached=()):
    B = arm.data.bones
    gen = {b.name for b in B if info[b.name]['kind'] == 'generic'}
    detached = set(detached)
    memo = {}
    scale = rig_scale(arm)
    if detached:
        # a detached bone's mirror twin starts its own chain too (keeps both sides symmetric)
        left = rig_axes(arm)[2]
        tol = 0.004 * scale
        for n in list(detached):
            if n not in B:
                continue
            mp = mirror_point(B[n].head_local, left)
            for b in B:
                if b.name in gen and b.name != n and (b.head_local - mp).length < tol:
                    detached.add(b.name)

    def ndesc(b):
        if b.name not in memo:
            memo[b.name] = sum(1 + ndesc(c) for c in b.children if c.name in gen)
        return memo[b.name]

    def incoming(b):
        """Direction the chain arrives from: parent->head inside a chain, otherwise
        the bone's own weight axis (for chain roots)."""
        p = b.parent
        if p is not None and p.name in gen:
            v = b.head_local - p.head_local
            if v.length > 1e-5 * scale:
                return v.normalized()
        if pts:
            wa = weight_axis(pts, b.name, b.head_local)
            if wa and wa[1] >= ELONGATED:
                return wa[0]
        return None

    def angle_to(b, c, ref):
        v = c.head_local - b.head_local
        if v.length <= 1e-5 * scale:
            return 0.0          # coincident child continues the bone
        return math.degrees(v.normalized().angle(ref))

    chosen = {}
    for b in B:
        if b.name in gen:
            # a detached bone starts its own chain: its parent may not continue into it
            kids = [c for c in b.children if c.name in gen and c.name not in detached]
            if not kids:
                continue
            ref = incoming(b) if len(kids) > 1 else None
            if ref is not None:
                # straightest continuation first; descendants only break ties
                kids.sort(key=lambda c: (round(angle_to(b, c, ref) / 5.0), -ndesc(c),
                                         (c.head_local - b.head_local).length))
            else:
                kids.sort(key=lambda c: (-ndesc(c), (c.head_local - b.head_local).length))
            chosen[b.name] = kids[0].name
    chains = []
    for b in B:
        if b.name not in gen:
            continue
        p = b.parent
        if p is None or p.name not in gen or chosen.get(p.name) != b.name:
            seq, cur = [b.name], b.name
            while cur in chosen:
                cur = chosen[cur]
                seq.append(cur)
            chains.append(seq)
    return chains, chosen


def attach_of(arm, info, root):
    p = arm.data.bones[root].parent
    while p is not None and info[p.name]['kind'] == 'generic':
        p = p.parent
    return p.name if p else ''


CATEGORY_ITEMS = [
    ('HAIR', 'Hair', 'Hair strands, curls, bangs'),
    ('SKIRT', 'Skirt', 'Skirt / dress panels'),
    ('CAPE', 'Cape', 'Capes, coat tails, back panels'),
    ('SLEEVE', 'Sleeve', 'Sleeve frills, cuffs'),
    ('RIBBON', 'Ribbon', 'Ribbons, bows, ties'),
    ('ACCESSORY', 'Accessory', 'Accessories (named acc_*)'),
    ('PROP', 'Prop', 'Weapons / held items'),
    ('FACE', 'Face', 'Extra face bones (named face_*)'),
    ('HAT', 'Hat / Helmet', 'Hats, helmets, crowns, hoods (named hat_*)'),
    ('OTHER', 'Other', 'Anything else (named extra_*)'),
    ('CUSTOM', 'Custom', 'Use the label as the whole name'),
    ('KEEP', 'Keep Names', 'Leave these bones named as they are'),
]
CAT_BASE = {'HAIR': 'hair', 'SKIRT': 'skirt', 'CAPE': 'cape', 'SLEEVE': 'sleeve',
            'RIBBON': 'ribbon', 'ACCESSORY': 'acc', 'PROP': 'prop', 'FACE': 'face', 'HAT': 'hat',
            'OTHER': 'extra'}


CORE_CATEGORIES = {
    'KEEP': 'already-clean rigs may get renamed again on re-analysis',
    'OTHER': 'unrecognised chains need a fallback (Keep Names is used instead)',
    'FACE': 'face parts are no longer detected by name',
    'HAIR': 'hair chains lose head-centred rolls',
    'PROP': 'weapons / held items get body-style rolls instead of prop rolls',
}


def all_categories():
    """[(id, label, description)] built-ins first, then the user's custom categories."""
    out = [(i, n, d) for i, n, d in CATEGORY_ITEMS]
    p = get_prefs()
    if p is not None:
        for c in p.custom_categories:
            tok = clean_token(c.token or c.name)
            if tok:
                out.append(('X_' + tok, c.name or tok, 'Custom category (bones named %s_*)' % tok))
    return out


def cat_enabled(cid):
    p = get_prefs()
    if p is None or cid.startswith('X_'):
        return True
    return getattr(p, 'cat_' + cid, True)


def enabled_fallback(cid):
    if cat_enabled(cid):
        return cid
    for c in ('OTHER', 'KEEP'):
        if cat_enabled(c):
            return c
    return next((i for i, _, _ in all_categories() if cat_enabled(i)), 'KEEP')


def cat_base(cid):
    if cid.startswith('X_'):
        return cid[2:]
    return CAT_BASE.get(cid, 'extra')


def _custom_cat(cid):
    p = get_prefs()
    if p is None or not cid.startswith('X_'):
        return None
    for c in p.custom_categories:
        if clean_token(c.token or c.name) == cid[2:]:
            return c
    return None


_CAT_ITEMS_KEEPALIVE = []


def _cat_items(self, context):
    cats = all_categories()
    cur = self.category
    items = []
    for k, (cid, name, desc) in enumerate(cats):
        if cat_enabled(cid) or cid == cur:
            items.append((cid, name if cat_enabled(cid) else name + ' (disabled)', desc, k + 1))
    if cur and cur not in [c[0] for c in cats]:
        items.append((cur, cur + ' (missing)', 'Category no longer exists', len(cats) + 1))
    _CAT_ITEMS_KEEPALIVE[:] = items      # Blender needs the strings kept alive
    return items


def _cat_get(self):
    cats = all_categories()
    for k, (cid, _, _) in enumerate(cats):
        if cid == self.category:
            return k + 1
    return len(cats) + 1


def _cat_set(self, value):
    cats = all_categories()
    if 1 <= value <= len(cats):
        self.category = cats[value - 1][0]
        _propagate(self, 'category')


def _letters(k):
    return chr(97 + k) if k < 26 else 'x%d' % k


def chain_names(s):
    """{bone: new name} for every chain bone, from the chain items."""
    items = list(s.chains)
    byroot = {it.root: it for it in items}
    stems = {}
    for it in items:
        if it.category == 'KEEP':
            continue
        lab = clean_token(it.label)
        if it.category == 'CUSTOM':
            base = lab or 'extra'
        else:
            base = cat_base(it.category) + ('_' + lab if lab else '')
        stems[it.root] = base
    groups = {}
    for it in items:
        if it.root not in stems:
            continue
        p = byroot.get(it.partner)
        if p and it.side == 'R' and stems.get(p.root) == stems[it.root]:
            continue  # represented by its left partner
        groups.setdefault(stems[it.root], []).append(it)
    letter = {}

    def trailing_num(root):
        m = re.search(r'(\d+)(?:[._ ]?[LRlr])?$', root)
        return int(m.group(1)) if m else None

    for base, lst in groups.items():
        if len(lst) > 1:
            nums = [trailing_num(it.root) for it in lst]
            if all(n is not None for n in nums) and len(set(nums)) == len(nums):
                lst.sort(key=lambda it: trailing_num(it.root))   # keep the original numbering
            else:
                lst.sort(key=lambda it: (round(it.angle / 10.0), -it.height, it.root))
            for k, it in enumerate(lst):
                letter[it.root] = _letters(k)
                p = byroot.get(it.partner)
                if p and stems.get(p.root) == stems[it.root]:
                    letter[p.root] = _letters(k)
    names = {}
    for it in items:
        bones = it.bones.split('\t')
        if it.root not in stems:
            for b in bones:
                names[b] = b
            continue
        stem = stems[it.root] + ('_' + letter[it.root] if it.root in letter else '')
        sx = suffix(s, it.side) if it.side != 'C' else ''
        for i, b in enumerate(bones):
            names[b] = stem + ('_%02d' % (i + 1) if len(bones) > 1 else '') + sx
    return names


def _affixes(text):
    # comma-separated list, longest first so 'Bone_L_' wins over 'Bone_'
    return sorted({t.strip() for t in text.split(',') if t.strip()}, key=len, reverse=True)


def strip_affixes(s, name):
    """Remove one matching prefix and/or suffix (if the option is on)."""
    if not s.opt_strip:
        return name
    out = name
    for pre in _affixes(s.strip_prefix):
        if out.startswith(pre) and len(out) > len(pre):
            out = out[len(pre):]
            break
    for suf in _affixes(s.strip_suffix):
        if out.endswith(suf) and len(out) > len(suf):
            out = out[:-len(suf)]
            break
    return out


def finalize_name(s, name):
    return translate_name(s, strip_affixes(s, name))


def refresh_previews(s):
    names = chain_names(s)
    for it in s.chains:
        if it.category == 'KEEP':
            st = finalize_name(s, it.root)
            it.preview = '(keeps names)' if st == it.root else st
        else:
            it.preview = finalize_name(s, names.get(it.root, '?'))


_SUSPEND = [False]


def _propagate(self, prop):
    """Copy one edited property to the other checked chains (multi-edit) and, for
    category/label, to every affected chain's mirror partner."""
    if _SUSPEND[0]:
        return
    _SUSPEND[0] = True
    try:
        s = self.id_data.rig_tidy
        val = getattr(self, prop)
        group = [self.root]
        if self.selected:
            group += [it.root for it in s.chains if it.selected and it.root != self.root]
        targets = set(group)
        if prop in ('category', 'label'):
            for r in group:
                it = _item(s, r)
                if it and it.partner:
                    targets.add(it.partner)
        for r in targets:
            it = _item(s, r)
            if it and r != self.root and getattr(it, prop) != val:
                setattr(it, prop, val)
        refresh_previews(s)
    finally:
        _SUSPEND[0] = False


def _on_cat_change(self, context):
    _propagate(self, 'category')


def _on_label_change(self, context):
    _propagate(self, 'label')


def _on_side_change(self, context):
    _propagate(self, 'side')


def _on_style_change(self, context):
    if not _SUSPEND[0]:
        refresh_previews(self)


# ---------------------------------------------------------------------------
# Body / face names
# ---------------------------------------------------------------------------

def body_face_names(arm, info, s):
    """{orig: new} for recognised bones (before swap fixes)."""
    B = arm.data.bones
    out = {}
    spines = sorted((i['idx'], n) for n, i in info.items() if i['role'] == 'spine')
    necks = sorted((i['idx'], n) for n, i in info.items() if i['role'] == 'neck')
    for k, (_, n) in enumerate(spines):
        out[n] = 'spine_%02d' % (k + 1)
    for k, (_, n) in enumerate(necks):
        out[n] = 'neck' if len(necks) == 1 else 'neck_%02d' % (k + 1)
    toes = {}
    twists = {}
    for n, i in info.items():
        role, side = i['role'], i['side']
        if i['kind'] == 'body':
            if role in ('spine', 'neck'):
                continue
            if role == 'root':
                out[n] = 'root'
            elif role == 'com':
                out[n] = 'COM'
            elif role in ('pelvis', 'head'):
                out[n] = role
            elif role == 'finger':
                f, seg = i['idx']
                out[n] = '%s_%02d%s' % (FINGERS[f] if f < 5 else 'finger%d' % f, seg + 1, suffix(s, side))
            elif role == 'toe':
                toes.setdefault(side, []).append((i['idx'], n))
            elif role == 'twist':
                twists.setdefault((i['idx'][0], side), []).append(n)
            elif role == 'helper':
                out[n] = '%s_helper%s' % (i['idx'], suffix(s, side))
            else:
                out[n] = role + suffix(s, side)
        elif i['kind'] == 'face':
            if role == 'tongue':
                out[n] = 'tongue_%02d' % (i['idx'] + 1)
            else:
                out[n] = role + suffix(s, side)
    for side, lst in toes.items():
        lst.sort()
        for k, (_, n) in enumerate(lst):
            out[n] = ('toe' if len(lst) == 1 else 'toe_%02d' % (k + 1)) + suffix(s, side)
    limb_bone = {(i['role'], i['side']): n for n, i in info.items() if i['kind'] == 'body'}
    for (limb, side), lst in twists.items():
        lb = limb_bone.get((limb, side))
        if lb:
            h0 = B[lb].head_local
            lst.sort(key=lambda n: (B[n].head_local - h0).length)
        for k, n in enumerate(lst):
            out[n] = '%s_twist%s%s' % (limb, '' if len(lst) == 1 else '_%02d' % (k + 1), suffix(s, side))
    return out


def find_swaps(arm, info, pts, base_names, scale, left):
    """Mirrored name pairs whose weights say they are swapped.
    Returns (swaps [(boneA, boneB)], warnings [str])."""
    inv = {v: k for k, v in base_names.items()}
    cen = {n: weighted_centroid(pts, n) for n in base_names}
    tol = 0.003 * scale

    def partner_name(new):
        for a, b in (('.L', '.R'), ('_L', '_R'), ('.l', '.r')):
            if new.endswith(a):
                return new[:-2] + b
            if new.endswith(b):
                return new[:-2] + a
        return None

    right = [o for o, nw in base_names.items() if cen.get(o) is not None
             and partner_name(nw) and nw[-1] in 'Rr']

    def best_right(o):
        c = cen.get(o)
        if c is None or not right:
            return None, None
        mc = mirror_point(c, left)
        b = min(right, key=lambda r: (cen[r] - mc).length)
        return b, (cen[b] - mc).length

    swaps, warns, done = [], [], set()
    for o, nw in base_names.items():
        if nw[-1] not in 'Ll' or not partner_name(nw) or o in done:
            continue
        exp = inv.get(partner_name(nw))
        if exp is None or cen.get(o) is None or cen.get(exp) is None:
            continue
        mc = mirror_point(cen[o], left)
        d_exp = (cen[exp] - mc).length
        best, d_best = best_right(o)
        if best is None or best == exp or d_exp <= d_best + tol:
            continue
        # is it a clean 2-way swap?
        o2 = inv.get(partner_name(base_names[best]))
        b2 = best_right(o2)[0] if o2 else None
        if b2 == exp and d_best < tol * 2:
            swaps.append((exp, best))
            done.update((o, o2))
        else:
            warns.append('%s: weights mirror %s better than %s' % (o, best, exp))
    return swaps, warns


LOWER_MOUTH = re.compile(r'(chin|teeth_lower|lip_lower(_\d+)?)')


def jaw_reparent_plan(arm, info):
    """(jaw, head_lower, [bones to parent under jaw]) for the lower mouth."""
    jaw = next((n for n, i in info.items() if i['role'] == 'jaw'), None)
    hl = next((n for n, i in info.items() if i['role'] == 'head_lower'), None)
    if not jaw:
        return None, None, []
    B = arm.data.bones
    above_jaw = {p.name for p in B[jaw].parent_recursive}
    move = []
    for n, i in info.items():
        if i['kind'] != 'face' or n == jaw or n in above_jaw:
            continue
        role = i['role']
        is_tongue_root = role == 'tongue' and i['idx'] == 0
        if LOWER_MOUTH.fullmatch(role) or is_tongue_root:
            p = B[n].parent
            if p is None or p.name != jaw:
                move.append(n)
    # only re-parent the jaw if it isn't already under head_lower (and that wouldn't make a loop)
    if hl:
        already = B[jaw].parent is not None and B[jaw].parent.name == hl
        loop = jaw in {p.name for p in B[hl].parent_recursive}
        if already or loop:
            hl = None
    return jaw, hl, move


def clavicle_reparent_plan(arm, info):
    """(top spine bone, [clavicles to parent under it])."""
    spines = sorted((i['idx'], n) for n, i in info.items() if i['role'] == 'spine')
    if not spines:
        return None, []
    top = spines[-1][1]
    B = arm.data.bones
    above_top = {p.name for p in B[top].parent_recursive}
    move = []
    for n, i in info.items():
        if i['role'] == 'clavicle' and n not in above_top:
            p = B[n].parent
            if p is None or p.name != top:
                move.append(n)
    return top, move


def leg_reparent_plan(arm, info):
    """(pelvis, [thighs to parent under it])."""
    pel = next((n for n, i in info.items() if i['role'] == 'pelvis'), None)
    if not pel:
        return None, []
    B = arm.data.bones
    above = {p.name for p in B[pel].parent_recursive}
    move = [n for n, i in info.items() if i['role'] == 'thigh' and n not in above
            and (B[n].parent is None or B[n].parent.name != pel)]
    return pel, move


COLLECTIONS = ['Body', 'Helpers', 'Face', 'Hair', 'Clothing', 'Props', 'Other']
CLOTHING_SUBS = ['Skirt', 'Cape', 'Sleeve', 'Ribbon', 'Accessory', 'Hat']
LEGACY_COLLECTIONS = ['Clothes']          # older Rig Tidy versions; emptied and removed
CAT_COLLECTION = {'HAIR': 'Hair', 'SKIRT': 'Skirt', 'CAPE': 'Cape', 'SLEEVE': 'Sleeve',
                  'RIBBON': 'Ribbon', 'ACCESSORY': 'Accessory', 'PROP': 'Props', 'FACE': 'Face',
                  'HAT': 'Hat',
                  'OTHER': 'Other', 'CUSTOM': 'Other'}
PREFIX_COLLECTION = [('breast', 'Body'), ('hair', 'Hair'), ('skirt', 'Skirt'), ('cape', 'Cape'),
                     ('sleeve', 'Sleeve'), ('ribbon', 'Ribbon'), ('acc', 'Accessory'),
                     ('prop', 'Props'), ('face', 'Face'), ('hat', 'Hat')]


def collection_parent(name):
    """Parent collection name for a Rig Tidy collection, or None for top level."""
    if name in CLOTHING_SUBS:
        return 'Clothing'
    p = get_prefs()
    if p is not None:
        for c in p.custom_categories:
            if (c.collection or 'Other').strip() == name:
                par = (getattr(c, 'parent', '') or '').strip()
                return par if par and par != name else None
    return None


def collection_for(info_entry, category, name):
    k = info_entry['kind']
    if k == 'body':
        return 'Helpers' if info_entry['role'] in ('twist', 'helper') else 'Body'
    if k == 'face':
        return 'Face'
    cc = _custom_cat(category) if category else None
    if cc is not None:
        return (cc.collection or 'Other').strip() or 'Other'
    if category and category not in ('KEEP', 'CUSTOM', 'OTHER'):
        return CAT_COLLECTION.get(category, 'Other')
    low = name.lower()
    p = get_prefs()
    prefixes = list(PREFIX_COLLECTION)
    if p is not None:
        prefixes += [(clean_token(c.token or c.name), (c.collection or 'Other').strip() or 'Other')
                     for c in p.custom_categories if clean_token(c.token or c.name)]
    for pre, col in prefixes:
        if low == pre or low.startswith(pre + '_') or low.startswith(pre + '.'):
            return col
    return 'Other'


# ---------------------------------------------------------------------------
# Orientation plan
# ---------------------------------------------------------------------------

def build_plan(arm, info, chosen, chain_info, pts, axes, scale, pairs=None):
    """{bone: (direction, length, roll_vector)} - heads never change."""
    up, fwd, left = axes
    B = arm.data.bones
    H = {b.name: b.head_local.copy() for b in B}
    T = {b.name: b.tail_local.copy() for b in B}
    tiny = 1e-5 * scale
    min_len = 0.003 * scale

    roles = {}
    for n, i in info.items():
        if i['kind'] != 'generic':
            roles.setdefault((i['role'], i['side'], i['idx']), n)

    def R(role, side=None, idx=0):
        return roles.get((role, side, idx))

    spines = [n for _, n in sorted((i['idx'], n) for n, i in info.items() if i['role'] == 'spine')]
    necks = [n for _, n in sorted((i['idx'], n) for n, i in info.items() if i['role'] == 'neck')]
    head = R('head')
    ups = [h.dot(up) for h in H.values()]
    height = max(max(ups) - min(ups), 1e-4)
    eL, eR = R('eye', 'L'), R('eye', 'R')
    eye_dist = (H[eL] - H[eR]).length if eL and eR else 0.0
    face_len = 0.15 * eye_dist if eye_dist > 0 else 0.008 * height
    if head and necks:
        head_len = (H[head] - H[necks[-1]]).length
    else:
        head_len = eye_dist * 1.4 if eye_dist > 0 else 0.06 * height
    head_len = max(head_len, min_len)

    def to(a, b):
        if not b or b not in H:
            return None
        v = H[b] - H[a]
        return (v.normalized(), v.length) if v.length > tiny else None

    def extent(n, h, d, default):
        lst = [c for c, w in pts.get(n, ()) if w > 0.25]
        if not lst:
            return default
        return max(max((c - h).dot(d) for c in lst), min_len)

    hand_n = {}
    for side in ('L', 'R'):
        hb = R('hand', side)
        if not hb:
            continue
        bases = [R('finger', side, (f, 0)) for f in range(1, 5)]
        bases = [b for b in bases if b]
        mid = R('finger', side, (2, 0)) or (bases[len(bases) // 2] if bases else None)
        out = left if side == 'L' else -left
        if mid and len(bases) >= 2:
            d = (H[mid] - H[hb]).normalized()
            a = (H[bases[0]] - H[bases[-1]]).normalized()
            n = a.cross(d)
            if n.length > 1e-6:
                n.normalize()
                hand_n[side] = n if n.dot(up + out) > 0 else -n
        hand_n.setdefault(side, (up + out).normalized())

    pelvis = R('pelvis') or R('com')
    body_center = H[pelvis] if pelvis else Vector()
    head_center = H[head] if head else body_center

    order = []

    def walk(b):
        order.append(b)
        for c in b.children:
            walk(c)
    for b in B:
        if b.parent is None:
            walk(b)

    plan = {}
    for b in order:
        n = b.name
        i = info[n]
        h = H[n]
        kind, role, side = i['kind'], i['role'], i['side']
        par = b.parent.name if b.parent else None
        pdir = plan[par][0] if par in plan else None
        plen = plan[par][1] if par in plan else None
        o = T[n] - h
        odir = o.normalized() if o.length > tiny else up
        olen = max(o.length, min_len)
        dl = None
        rv = fwd

        def leaf(prefer_cont):
            if prefer_cont and pdir is not None:
                d = pdir
            else:
                c = weighted_centroid(pts, n)
                if c is not None and (c - h).length > 10 * tiny:
                    d = (c - h).normalized()
                else:
                    d = pdir if pdir is not None else odir
            default = plen * 0.6 if plen else olen
            return d, extent(n, h, d, default)

        if kind == 'body':
            if role == 'root':
                dl = (up, 0.08 * height)
            elif role == 'com':
                dl = (up, 0.04 * height)
            elif role == 'pelvis':
                dl = to(n, (spines or necks or [head])[0])
            elif role == 'spine':
                k = spines.index(n)
                dl = to(n, spines[k + 1] if k + 1 < len(spines) else (necks[0] if necks else head))
            elif role == 'neck':
                k = necks.index(n)
                dl = to(n, necks[k + 1] if k + 1 < len(necks) else head)
            elif role == 'head':
                dl = (up, head_len)
            elif role in ('clavicle', 'upper_arm', 'forearm', 'thigh', 'shin'):
                nxt = {'clavicle': 'upper_arm', 'upper_arm': 'forearm', 'forearm': 'hand',
                       'thigh': 'shin', 'shin': 'foot'}[role]
                dl = to(n, R(nxt, side))
            elif role == 'hand':
                mid = R('finger', side, (2, 0))
                dl = to(n, mid)
                if not dl:
                    bases = [R('finger', side, (f, 0)) for f in range(1, 5)]
                    bases = [x for x in bases if x]
                    if bases:
                        avg = sum((H[x] for x in bases), Vector()) / len(bases)
                        v = avg - h
                        if v.length > tiny:
                            dl = (v.normalized(), v.length)
                rv = hand_n.get(side, fwd)
            elif role == 'foot':
                dl = to(n, R('toe', side, 0))
                rv = up
            elif role == 'toe':
                dl = to(n, R('toe', side, i['idx'] + 1))
                if not dl:
                    d = (fwd - fwd.dot(up) * up).normalized()
                    dl = (d, extent(n, h, d, (plen or olen) * 0.5))
                rv = up
            elif role == 'finger':
                f, seg = i['idx']
                dl = to(n, R('finger', side, (f, seg + 1)))
                if not dl:
                    dl = leaf(True)
                rv = hand_n.get(side, fwd)
            elif role == 'twist':
                limb = i['idx'][0]
                a, c = R(limb, side), R(LIMB_CHILD[limb], side)
                if a and c and (H[c] - H[a]).length > tiny:
                    ld = H[c] - H[a]
                    llen = ld.length
                    ld.normalize()
                    rem = (H[c] - h).dot(ld)
                    L = rem * 0.5 if rem > 0.6 * llen else max(rem, 0.25 * llen)
                    dl = (ld, max(L, min_len))
            elif role == 'helper':
                limb = 'shin' if i['idx'] == 'knee' else 'forearm'
                a, c = R(limb, side), R(LIMB_CHILD[limb], side)
                if a and c and (H[c] - H[a]).length > tiny:
                    v = H[c] - H[a]
                    dl = (v.normalized(), 0.15 * v.length)
            if not dl:
                dl = leaf(True)

        elif kind == 'face':
            rv = up
            if role == 'head_upper':
                dl = (up, 0.8 * head_len)
                rv = fwd
            elif role in ('head_lower', 'jaw'):
                dl = to(n, R('chin'))
                if not dl:
                    dl = ((fwd - up).normalized(), 0.5 * head_len)
            elif role in ('eye', 'eyeball') or role.startswith('eye_star'):
                dl = (fwd, face_len * 1.6)
            elif role.startswith('lid'):
                c = weighted_centroid(pts, n)
                if c is not None and (c - h).length > 10 * tiny:
                    dl = ((c - h).normalized(), (c - h).length)
                else:
                    dl = (fwd, face_len)
            elif role == 'tongue':
                dl = to(n, R('tongue', None, i['idx'] + 1)) or (fwd, face_len)
            else:
                dl = (fwd, face_len)

        else:  # generic chain bone
            cat, center = chain_info.get(n, ('OTHER', body_center))
            child = chosen.get(n)
            gen_kids = [c for c in b.children if info[c.name]['kind'] == 'generic']
            chain_root = par is None or info[par]['kind'] != 'generic'
            if chain_root and len(gen_kids) > 1:
                # a branching root (e.g. a weapon with ribbons): follow its own weights
                wa = weight_axis(pts, n, h)
                if wa and wa[1] >= ELONGATED:
                    dl = (wa[0], extent(n, h, wa[0], olen))
            if dl:
                pass
            elif child:
                dl = to(n, child) or (odir, olen)
            else:
                # chain end: if its own weights clearly point somewhere (e.g. a pigtail tip
                # curling back), aim along them; otherwise continue the chain
                wa = weight_axis(pts, n, h)
                if wa:
                    ax, ratio, cen = wa
                    off = cen - h
                    ref = plen or olen
                    if ratio >= ELONGATED and off.length > 10 * tiny and \
                            math.degrees(ax.angle(off)) < 40.0:
                        dl = (ax, extent(n, h, ax, olen))
                    elif off.length > max(0.3 * ref, 10 * tiny):
                        d = off.normalized()
                        dl = (d, extent(n, h, d, olen))
                if not dl:
                    dl = leaf(par is not None and chosen.get(par) == n)
            if cat == 'PROP':
                rv = fwd
            else:
                r = h - center
                r = r - r.dot(up) * up
                rv = r.normalized() if r.length > 0.01 * scale else fwd
                if abs(rv.dot(dl[0])) > 0.8:
                    rv = up

        d, L = dl
        d = d.normalized()
        L = max(L, min_len)
        rv = rv.normalized()
        if abs(rv.dot(d)) > 0.9:
            rv = up if abs(d.dot(up)) < 0.9 else fwd
        plan[n] = (d, L, rv)

    # mirrored pairs come out exactly mirrored (averages out lopsided weights)
    for a, b in (pairs or ()):
        if a not in plan or b not in plan:
            continue
        da, La, ra = plan[a]
        db, Lb, rb = plan[b]
        d = da + mirror_point(db, left)
        d = d.normalized() if d.length > 1e-6 else da
        rv = ra + mirror_point(rb, left)
        rv = rv.normalized() if rv.length > 1e-6 else ra
        if abs(rv.dot(d)) > 0.9:
            rv = up if abs(d.dot(up)) < 0.9 else fwd
        L = 0.5 * (La + Lb)
        plan[a] = (d, L, rv)
        plan[b] = (mirror_point(d, left), L, mirror_point(rv, left))
    return plan


FLIP_SUFFIX = [('.L', '.R'), ('_L', '_R'), ('.l', '.r'), ('_l', '_r')]
FLIP_TOKEN = [('Left', 'Right'), (' L ', ' R '), ('Bone_L', 'Bone_R'), ('Lstar', 'Rstar'), ('_L_', '_R_')]


def flip_name(n):
    for a, b in FLIP_SUFFIX:
        if n.endswith(a):
            return n[:-len(a)] + b
        if n.endswith(b):
            return n[:-len(b)] + a
    for a, b in FLIP_TOKEN:
        if a in n:
            return n.replace(a, b)
        if b in n:
            return n.replace(b, a)
    return None


def mirror_pairs(arm, s, left, scale):
    """[(left bone, right bone)] for bones whose names AND heads mirror
    (plus analysed chain partners, bone by bone)."""
    B = arm.data.bones
    tol = 0.004 * scale
    cands = set()
    for b in B:
        f = flip_name(b.name)
        if f and f in B and f != b.name:
            cands.add(frozenset((b.name, f)))
    byroot = {it.root: it for it in s.chains}
    for it in s.chains:
        p = byroot.get(it.partner)
        if p:
            for x, y in zip(it.bones.split('\t'), p.bones.split('\t')):
                cands.add(frozenset((x, y)))
    out = []
    for pr in cands:
        if len(pr) != 2:
            continue
        x, y = tuple(pr)
        hx, hy = B[x].head_local, B[y].head_local
        if (mirror_point(hx, left) - hy).length > tol:
            continue
        out.append((x, y) if hx.dot(left) > 0 else (y, x))
    return out


def _item(s, root):
    for it in s.chains:
        if it.root == root:
            return it
    return None


def split_to_partner(arm, s, a_root, b_root):
    """If two partnered chains differ in length, cut the longer one at the shorter one's
    length; the leftover bones become their own chain (e.g. bells hanging off an ear).
    Works by root names: adding to a collection can invalidate item references."""
    a, b = _item(s, a_root), _item(s, b_root)
    if not a or not b or a.count == b.count:
        return None
    longer, shorter = (a, b) if a.count > b.count else (b, a)
    seq = longer.bones.split('\t')
    keep, rest = seq[:shorter.count], seq[shorter.count:]
    lroot = longer.root
    vals = dict(side=longer.side, lat=longer.lat, angle=longer.angle, attach=longer.attach,
                category=longer.category, label=clean_token(longer.label))
    longer.bones = '\t'.join(keep)
    longer.count = len(keep)
    new = s.chains.add()
    new.root = new.name = rest[0]
    new.bones = '\t'.join(rest)
    new.count = len(rest)
    new.attach = vals['attach']
    new.side = vals['side']
    new.lat = vals['lat']
    new.angle = vals['angle']
    new.height = arm.data.bones[rest[0]].head_local.dot(rig_axes(arm)[0])
    new.category = vals['category']
    new.label = (vals['label'] + '_extra') if vals['label'] else 'extra'
    new.split_from = lroot
    src = next(k for k, it in enumerate(s.chains) if it.root == lroot)
    s.chains.move(len(s.chains) - 1, src + 1)
    return rest[0]


def unsplit(s, root):
    """Undo split_to_partner for a chain whose pairing was removed."""
    idx = [k for k, x in enumerate(s.chains) if x.split_from == root and not x.partner]
    if not idx:
        return
    it = _item(s, root)
    if it is None:
        return
    for k in idx:
        x = s.chains[k]
        it.bones = it.bones + '\t' + x.bones
        it.count += x.count
    for k in reversed(idx):
        s.chains.remove(k)


def _on_partner_change(self, context):
    if _SUSPEND[0]:
        return
    _SUSPEND[0] = True
    try:
        data = self.id_data
        s = data.rig_tidy
        me, new = self.root, self.partner
        unsplit(s, me)
        for r in [it.root for it in s.chains if it.root != new and it.partner == me]:
            _item(s, r).partner = ''
            unsplit(s, r)
        p = _item(s, new) if new else None
        if p is None or new == me:
            _item(s, me).partner = ''
        else:
            oldr = p.partner
            if oldr and oldr != me and _item(s, oldr):
                _item(s, oldr).partner = ''
                unsplit(s, oldr)
            unsplit(s, new)
            a, p = _item(s, me), _item(s, new)
            p.partner = me
            if a.lat * p.lat < 0:        # opposite sides of the body
                a.side = 'L' if a.lat > 0 else 'R'
                p.side = 'L' if p.lat > 0 else 'R'
            p.category = a.category
            p.label = a.label
            arm = next((o for o in bpy.data.objects
                        if o.type == 'ARMATURE' and o.data == data), None)
            if arm:
                split_to_partner(arm, s, me, new)
        refresh_previews(s)
        # splitting / un-splitting shifts the list: stay on the chain the user is editing
        k = next((k for k, it in enumerate(s.chains) if it.root == me), None)
        if k is not None and s.index != k:
            s.index = k
    finally:
        _SUSPEND[0] = False


# ---------------------------------------------------------------------------
# Properties
# ---------------------------------------------------------------------------

class RigTidyChain(bpy.types.PropertyGroup):
    root: StringProperty()
    bones: StringProperty()
    count: IntProperty()
    category: StringProperty(default='OTHER')          # category id (built-in or X_custom)
    category_ui: EnumProperty(name='Category', items=_cat_items, get=_cat_get, set=_cat_set,
                              description='What this chain is (set in Preferences)')
    label: StringProperty(name='Label', description='Extra word in the name, e.g. front, curl',
                          update=_on_label_change)
    side: EnumProperty(name='Side', items=[('L', 'Left', ''), ('R', 'Right', ''), ('C', 'Center', '')],
                       default='C', update=_on_side_change)
    partner: StringProperty(name='Mirror', description='Mirror partner chain (picking one links both ways)',
                            update=lambda self, ctx: _on_partner_change(self, ctx))
    attach: StringProperty()
    preview: StringProperty()
    angle: FloatProperty()
    height: FloatProperty()
    testing: BoolProperty()
    lat: FloatProperty()
    split_from: StringProperty()
    selected: BoolProperty(name='Selected', description='Include in multi-edit')


class RigTidySettings(bpy.types.PropertyGroup):
    chains: CollectionProperty(type=RigTidyChain)
    index: IntProperty()
    analyzed: BoolProperty()
    report: StringProperty()
    suffix_style: EnumProperty(name='Side Suffix', items=[
        ('DOT', '.L / .R', 'Blender default (mirror tools understand it)'),
        ('UNDER', '_L / _R', ''),
        ('DOTLOW', '.l / .r', '')], default='DOT', update=_on_style_change)
    opt_backup: BoolProperty(name='Save backup copy', default=True,
                             description='Save a copy of the .blend next to it before changing anything')
    opt_swaps: BoolProperty(name='Fix swapped L/R names', default=True)
    opt_body: BoolProperty(name='Rename body', default=True)
    opt_face: BoolProperty(name='Rename face', default=True)
    opt_chains: BoolProperty(name='Rename chains', default=True)
    opt_orient: BoolProperty(name='Fix directions & rolls', default=True)
    opt_jaw: BoolProperty(name='Parent lower mouth to jaw', default=True,
                          description='Chin, lower lips, lower teeth and tongue follow the jaw; '
                                      'jaw is parented to head_lower')
    opt_legs: BoolProperty(name='Parent thighs to pelvis', default=True,
                           description='Thighs follow the pelvis instead of a spine bone')
    opt_clavicle: BoolProperty(name='Parent clavicles to top spine', default=True,
                               description='Clavicles follow the uppermost spine bone instead of the neck')
    opt_collections: BoolProperty(name='Make bone collections', default=True,
                                  description='Sort bones into Body, Helpers, Face, Hair, Clothes, Props '
                                              'and Other collections')
    opt_pose: BoolProperty(name='Clear import pose noise', default=True,
                           description='Zero tiny leftover pose values (real pose offsets are kept)')
    show_options: BoolProperty(default=True)
    opt_auto_face: BoolProperty(name='Detect face parts & headwear by name', default=True,
                                description='On Analyze, chains on the head named like eye / jaw / chin / '
                                            'nose / cheek / mouth / lip / teeth (or hat / helmet / crown) '
                                            'get the Face or Hat category')
    opt_translate: BoolProperty(name='Translate names (dictionary)', default=True,
                                description='Swap words found in dictionary.csv (e.g. pinyin) for English '
                                            'in final bone names',
                                update=_on_style_change)
    opt_strip: BoolProperty(name='Strip prefix / suffix', default=False,
                            description='Remove a prefix and/or suffix from final bone names',
                            update=_on_style_change)
    strip_prefix: StringProperty(name='Prefix', default='Bone_',
                                 description='Comma-separated, e.g. "Bone_, BN_"',
                                 update=_on_style_change)
    strip_suffix: StringProperty(name='Suffix', default='',
                                 description='Comma-separated, e.g. "_SkinJNT, _jnt"',
                                 update=_on_style_change)
    detached: StringProperty(description='Bones the user split out of their chains (tab separated)')
    live_select: BoolProperty(name='Live Select', default=False,
                              description='Clicking a bone in the viewport highlights its chain in the list')


# ---------------------------------------------------------------------------
# Operators
# ---------------------------------------------------------------------------

def _arm(context):
    o = context.object
    return o if o and o.type == 'ARMATURE' else None


def _pose_sig(pb, scale):
    loc, rot, sc = pb.matrix_basis.decompose()
    ang = math.degrees(rot.angle) if rot.angle <= math.pi else 360 - math.degrees(rot.angle)
    return ang > 0.01 or loc.length > 1e-5 * scale or max(abs(x - 1) for x in sc) > 1e-4


class RIGTIDY_OT_analyze(bpy.types.Operator):
    bl_idname = 'rig_tidy.analyze'
    bl_label = 'Analyze Rig'
    bl_description = 'Recognise bones, find unnamed chains and check L/R names against the weights'
    bl_options = {'REGISTER', 'UNDO'}

    keep_edits: BoolProperty(default=False, options={'HIDDEN', 'SKIP_SAVE'})

    @classmethod
    def poll(cls, context):
        return _arm(context) is not None

    def execute(self, context):
        arm = _arm(context)
        s = arm.data.rig_tidy
        _reset_tests(arm)
        saved = {}
        active_root = None
        if self.keep_edits:
            saved = {it.root: (it.category, it.label, it.side, it.partner, it.selected) for it in s.chains}
            if 0 <= s.index < len(s.chains):
                active_root = s.chains[s.index].root
            act = arm.data.bones.active
            if act is not None:
                active_root = act.name if any(act.name in it.bones.split('\t') for it in s.chains) \
                    else active_root
        info = classify(arm)
        B = arm.data.bones
        detached = [n for n in s.detached.split('\t') if n and n in B]
        s.detached = '\t'.join(detached)
        up, fwd, left = axes = rig_axes(arm)
        scale = rig_scale(arm)
        pts, owners = gather_weights(arm)
        chains, chosen = detect_chains(arm, info, pts, detached)
        tol = 0.004 * scale

        roles = {(i['role'], i['side']): n for n, i in info.items() if i['kind'] != 'generic'}
        pelvis = roles.get(('pelvis', None)) or roles.get(('com', None))
        necks = sorted((i['idx'], n) for n, i in info.items() if i['role'] == 'neck')
        neck = necks[0][1] if necks else roles.get(('head', None))
        head = roles.get(('head', None))
        body_c = B[pelvis].head_local if pelvis else Vector()
        head_c = B[head].head_local if head else body_c

        _SUSPEND[0] = True
        try:
            s.chains.clear()
            roots = []
            for seq in chains:
                it = s.chains.add()
                it.root = seq[0]
                it.name = seq[0]
                it.bones = '\t'.join(seq)
                it.count = len(seq)
                it.attach = attach_of(arm, info, seq[0])
                roots.append(it)
            # mirror partners + sides
            heads = {it.root: B[it.root].head_local.copy() for it in s.chains}
            # candidate pairs: mirrored roots; prefer mirrored names, then equal length, then distance
            pairs = []
            for it in s.chains:
                h = heads[it.root]
                lat = h.dot(left)
                it.lat = lat
                it.side = 'C' if abs(lat) <= tol else ('L' if lat > 0 else 'R')
                if lat <= tol:
                    continue                      # build each pair once, from the left side
                mp = mirror_point(h, left)
                flipped = flip_name(it.root)
                for ot in s.chains:
                    if ot.root == it.root:
                        continue
                    dist = (heads[ot.root] - mp).length
                    if dist < tol:
                        pairs.append(((flipped != ot.root, ot.count != it.count, dist),
                                      it.root, ot.root))
            pairs.sort()
            taken = set()
            for _, a, b in pairs:                 # one partner per chain
                if a in taken or b in taken:
                    continue
                _item(s, a).partner = b
                _item(s, b).partner = a
                taken.update((a, b))
            for r, pr in [(it.root, it.partner) for it in s.chains if it.partner and it.side == 'L']:
                split_to_partner(arm, s, r, pr)
            heads = {it.root: B[it.root].head_local.copy() for it in s.chains}
            # category / label guesses
            label_override = {}
            for it in s.chains:
                seq = it.bones.split('\t')
                h = heads[it.root]
                meshes = set().union(*[owners.get(n, set()) for n in seq])
                ai = info.get(it.attach) if it.attach else None
                arole = ai['role'] if ai else None
                head_attached = bool(ai) and (arole in HEAD_ROLES or arole == 'neck' or ai['kind'] == 'face')
                tidy_already = re.match(r'(face|hat)(?:[_.]|$)', it.root) is not None
                np_ = (name_part(it.root) if s.opt_auto_face and head_attached and not tidy_already
                       else None)
                if np_:
                    cat = np_[0]
                    label_override[it.root] = np_[1]
                elif not GENERIC_NAME.fullmatch(it.root):
                    cat = 'KEEP'
                elif any(PROP_MESH.search(m) for m in meshes) or not it.attach:
                    cat = 'PROP'
                elif ai and (arole in HEAD_ROLES or ai['kind'] == 'face'):
                    cat = 'HAIR'
                elif arole in ARM_ROLES or arole == 'twist' and ai['idx'][0] in ('upper_arm', 'forearm'):
                    cat = 'SLEEVE'
                elif arole in LEG_ROLES or arole == 'twist':
                    cat = 'ACCESSORY'
                elif arole in TORSO_ROLES and pelvis and neck:
                    p_up, n_up = body_c.dot(up), B[neck].head_local.dot(up)
                    t = (h.dot(up) - p_up) / max(n_up - p_up, 1e-6)
                    ahead = (h - B[it.attach].head_local).dot(fwd)
                    if t > 0.6 and it.count <= 2 and it.side != 'C' and ahead > 0.02 * scale:
                        cat = 'CUSTOM'      # short, paired, in front of the chest
                        label_override[it.root] = 'breast'
                    else:
                        cat = 'CAPE' if t > 0.6 else 'SKIRT'
                else:
                    cat = 'OTHER'
                cat = enabled_fallback(cat)
                center = head_c if cat == 'HAIR' else body_c
                r = h - center
                r = r - r.dot(up) * up
                ang = math.degrees(math.atan2(abs(r.dot(left)), r.dot(fwd))) if r.length > 1e-6 else 0.0
                it.angle = ang
                it.height = h.dot(up)
                it.category = cat
                if it.root in label_override:
                    it.label = label_override[it.root]
                elif cat in ('HAIR', 'SKIRT', 'CAPE'):
                    it.label = 'front' if ang < 50 else ('side' if ang < 130 else 'back')
                elif cat in ('SLEEVE', 'ACCESSORY') and arole:
                    it.label = 'twist' if arole == 'twist' else arole
                else:
                    it.label = ''
            # unpaired props default to no side suffix
            for it in s.chains:
                if it.category in ('PROP', 'HAT') and not it.partner:
                    it.side = 'C'
            # restore the user's edits (when re-analysing after Detach)
            if saved:
                for it in s.chains:
                    if it.root in saved:
                        it.category, it.label, it.side, _, it.selected = saved[it.root]
                for r, (_, _, _, pr, _) in saved.items():
                    a, p = _item(s, r), _item(s, pr) if pr else None
                    if a and p and a.partner != pr:
                        for x in (a.partner, p.partner):
                            if x and _item(s, x):
                                _item(s, x).partner = ''
                        a, p = _item(s, r), _item(s, pr)
                        a.partner, p.partner = pr, r
                        split_to_partner(arm, s, r, pr)
            refresh_previews(s)
        finally:
            _SUSPEND[0] = False

        base = body_face_names(arm, info, s)
        swaps, warns = find_swaps(arm, info, pts, base, scale, left)
        nb = sum(1 for i in info.values() if i['kind'] == 'body')
        nf = sum(1 for i in info.values() if i['kind'] == 'face')
        posed = [pb.name for pb in arm.pose.bones if _pose_sig(pb, scale)]
        lines = ['Body bones: %d   Face bones: %d' % (nb, nf),
                 'Unnamed chains: %d (%d bones)' % (len(s.chains), sum(it.count for it in s.chains))]
        for a, b in swaps:
            lines.append('Swapped names: %s <-> %s' % (a, b))
        for w in warns[:6]:
            lines.append('Check: ' + w)
        if posed:
            lines.append('Posed bones: %d (kept on Apply)' % len(posed))
        jaw, jaw_hl, jaw_move = jaw_reparent_plan(arm, info)
        if jaw_move or jaw_hl:
            lines.append('Lower mouth: %d bone(s) to parent under jaw' % len(jaw_move))
        top, cl_move = clavicle_reparent_plan(arm, info)
        if cl_move:
            lines.append('Clavicles: %d to parent under %s' % (len(cl_move), top))
        pel, leg_move = leg_reparent_plan(arm, info)
        if leg_move:
            lines.append('Thighs: %d to parent under %s' % (len(leg_move), pel))
        if detached:
            lines.append('Detached bones: %d' % len(detached))
        if arm.animation_data and arm.animation_data.action:
            lines.append('WARNING: armature has an action - it will not match new rest pose')
        if not bpy.data.filepath:
            lines.append('File not saved - no backup copy possible (use Ctrl+Z)')
        s.report = '\n'.join(lines)
        s.analyzed = True
        if not self.keep_edits:
            s.index = 0
        elif active_root:
            k = next((k for k, it in enumerate(s.chains) if active_root in it.bones.split('\t')), None)
            if k is not None:
                s.index = k
        s.index = min(s.index, max(len(s.chains) - 1, 0))
        self.report({'INFO'}, 'Rig Tidy: %d chains found' % len(s.chains))
        return {'FINISHED'}


def _chain_bones(it):
    return it.bones.split('\t') if it.bones else []


_TEST_STORE = {}


def _reset_tests(arm):
    store = _TEST_STORE.pop(arm.name, None)
    if store:
        for n, m in store.items():
            pb = arm.pose.bones.get(n)
            if pb:
                pb.matrix_basis = m
    s = arm.data.rig_tidy
    for it in s.chains:
        it.testing = False


class RIGTIDY_OT_select_chain(bpy.types.Operator):
    bl_idname = 'rig_tidy.select_chain'
    bl_label = 'Select'
    bl_description = 'Select this chain (and its mirror partner) in Pose Mode'
    bl_options = {'REGISTER', 'UNDO'}
    index: IntProperty()

    def execute(self, context):
        arm = _arm(context)
        s = arm.data.rig_tidy
        it = s.chains[self.index]
        group = [x for x in s.chains if x.selected] or [it]
        roots = {x.root for x in group} | {x.partner for x in group if x.partner}
        names = set()
        for x in s.chains:
            if x.root in roots:
                names |= set(_chain_bones(x))
        if context.mode != 'POSE':
            bpy.ops.object.mode_set(mode='POSE')
        for pb in arm.pose.bones:
            _set_select(pb, pb.name in names)
        arm.data.bones.active = arm.data.bones[it.root]
        return {'FINISHED'}


def _set_select(pb, state):
    try:
        pb.select = state
    except AttributeError:
        pb.bone.select = state


class RIGTIDY_OT_check(bpy.types.Operator):
    bl_idname = 'rig_tidy.check'
    bl_label = 'Check Chains'
    bl_description = 'Check / uncheck chains for multi-edit'
    bl_options = {'REGISTER', 'UNDO'}
    action: EnumProperty(items=[('ALL', 'All', ''), ('NONE', 'None', ''), ('INVERT', 'Invert', '')])

    def execute(self, context):
        s = _arm(context).data.rig_tidy
        for it in s.chains:
            it.selected = {'ALL': True, 'NONE': False}.get(self.action, not it.selected)
        return {'FINISHED'}


class RIGTIDY_OT_detach(bpy.types.Operator):
    bl_idname = 'rig_tidy.detach'
    bl_label = 'Detach Selected Bones'
    bl_description = ('Start a new chain at each bone selected in the viewport (it and the bones '
                      'below it leave their parent chain). Click again on detached bones to re-attach')
    bl_options = {'REGISTER', 'UNDO'}
    clear: BoolProperty(default=False, options={'SKIP_SAVE'})

    @classmethod
    def poll(cls, context):
        a = _arm(context)
        return a is not None and a.data.rig_tidy.analyzed

    def execute(self, context):
        arm = _arm(context)
        s = arm.data.rig_tidy
        cur = [n for n in s.detached.split('\t') if n]
        if self.clear:
            new = []
        else:
            chain = {n for it in s.chains for n in _chain_bones(it)}
            sel = [n for n in _selected_bones(arm) if n in chain]
            if not sel:
                self.report({'WARNING'}, 'Select chain bones in the viewport (Pose or Edit Mode)')
                return {'CANCELLED'}
            if all(n in cur for n in sel):          # all already detached -> re-attach
                new = [n for n in cur if n not in sel]
            else:
                new = cur + [n for n in sel if n not in cur]
        s.detached = '\t'.join(new)
        bpy.ops.rig_tidy.analyze(keep_edits=True)
        self.report({'INFO'}, 'Rig Tidy: %d detached bone(s)' % len(new))
        return {'FINISHED'}


def _selected_bones(arm):
    if arm.mode == 'EDIT':
        return [e.name for e in arm.data.edit_bones if e.select]
    out = []
    for pb in arm.pose.bones:
        try:
            on = pb.select
        except AttributeError:
            on = pb.bone.select
        if on:
            out.append(pb.name)
    return out


class RIGTIDY_OT_test_chain(bpy.types.Operator):
    bl_idname = 'rig_tidy.test_chain'
    bl_label = 'Test'
    bl_description = 'Swing this chain (and its partner) so you can see what it moves. Click again to reset'
    bl_options = {'REGISTER', 'UNDO'}
    index: IntProperty()

    def execute(self, context):
        arm = _arm(context)
        s = arm.data.rig_tidy
        it = s.chains[self.index]
        group = [it] + [ot for ot in s.chains if ot.root == it.partner]
        store = _TEST_STORE.setdefault(arm.name, {})
        turn_on = not it.testing
        for g in group:
            for n in _chain_bones(g):
                pb = arm.pose.bones[n]
                if turn_on:
                    store.setdefault(n, pb.matrix_basis.copy())
                    pb.matrix_basis = store[n] @ Matrix.Rotation(math.radians(25), 4, 'X')
                elif n in store:
                    pb.matrix_basis = store.pop(n)
            g.testing = turn_on
        context.view_layer.update()
        return {'FINISHED'}


def _roles_any(arm, s):
    """Role lookup that understands both Biped and Rig Tidy names."""
    out = {}
    for b in arm.data.bones:
        r = parse_body(b.name) or parse_face(b.name)
        if r:
            out[(r[0], r[1], r[2])] = b.name
            continue
        t = parse_tidy(b.name)
        if t:
            out[(t[1], t[2], t[3])] = b.name
    return out


class RIGTIDY_OT_test_pose(bpy.types.Operator):
    bl_idname = 'rig_tidy.test_pose'
    bl_label = 'Test Pose'
    bl_description = 'Toggle a symmetric test pose (elbows, knees, fingers, head, jaw)'
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        arm = _arm(context)
        key = arm.name + '#pose'
        if key in _TEST_STORE:
            for n, m in _TEST_STORE.pop(key).items():
                if n in arm.pose.bones:
                    arm.pose.bones[n].matrix_basis = m
            context.view_layer.update()
            return {'FINISHED'}
        roles = _roles_any(arm, arm.data.rig_tidy)
        store = {pb.name: pb.matrix_basis.copy() for pb in arm.pose.bones}
        _TEST_STORE[key] = store

        def rot(n, deg):
            if n:
                arm.pose.bones[n].matrix_basis = store[n] @ Matrix.Rotation(math.radians(deg), 4, 'X')
        for side in ('L', 'R'):
            rot(roles.get(('forearm', side, 0)), 60)
            rot(roles.get(('thigh', side, 0)), 30)
            rot(roles.get(('shin', side, 0)), -45)
            for f in range(5):
                for seg in range(4):
                    rot(roles.get(('finger', side, (f, seg))), 40 if f else 20)
        rot(roles.get(('head', None, 0)), -15)
        rot(roles.get(('jaw', None, 0)), -15)
        context.view_layer.update()
        return {'FINISHED'}


class RIGTIDY_OT_apply(bpy.types.Operator):
    bl_idname = 'rig_tidy.apply'
    bl_label = 'Apply Cleanup'
    bl_description = 'Fix directions, rolls and names using the settings above'
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        a = _arm(context)
        return a is not None and a.data.rig_tidy.analyzed

    def execute(self, context):
        arm = _arm(context)
        A = arm.data
        s = A.rig_tidy
        _reset_tests(arm)
        tkey = arm.name + '#pose'
        if tkey in _TEST_STORE:
            for n, m in _TEST_STORE.pop(tkey).items():
                if n in arm.pose.bones:
                    arm.pose.bones[n].matrix_basis = m

        info = classify(arm)
        names_now = {b.name for b in A.bones}
        chain_bones = set()
        for it in s.chains:
            for n in _chain_bones(it):
                if n not in names_now:
                    self.report({'ERROR'}, 'Rig changed since Analyze - run Analyze again')
                    return {'CANCELLED'}
                chain_bones.add(n)
        gen = {n for n, i in info.items() if i['kind'] == 'generic'}
        if gen != chain_bones:
            self.report({'ERROR'}, 'Rig changed since Analyze - run Analyze again')
            return {'CANCELLED'}

        up, fwd, left = axes = rig_axes(arm)
        scale = rig_scale(arm)
        msgs = []

        # 1. names (validated before anything is written)
        base = body_face_names(arm, info, s)
        pts, owners = gather_weights(arm)
        swaps = []
        if s.opt_swaps:
            swaps, _ = find_swaps(arm, info, pts, base, scale, left)
        final = {}
        for o, nw in base.items():
            k = info[o]['kind']
            if (k == 'body' and s.opt_body) or (k == 'face' and s.opt_face):
                final[o] = nw
        for a, b in swaps:
            ka, kb = info[a]['kind'], info[b]['kind']
            renaming = (ka == 'body' and s.opt_body) or (ka == 'face' and s.opt_face)
            if renaming:
                final[a], final[b] = base[b], base[a]
            else:
                final[a], final[b] = b, a
        if s.opt_chains:
            for o, nw in chain_names(s).items():
                if nw != o:
                    final[o] = nw
        result = {b.name: finalize_name(s, final.get(b.name, b.name)) for b in A.bones}
        seen = {}
        for o, nw in result.items():
            if nw in seen:
                self.report({'ERROR'}, 'Two bones would be named "%s" (%s, %s) - change a chain label'
                            % (nw, seen[nw], o))
                return {'CANCELLED'}
            seen[nw] = o

        # 2. backup (only once every check has passed)
        if s.opt_backup and bpy.data.filepath:
            root, ext = os.path.splitext(bpy.data.filepath)
            path = root + '_before_rig_tidy.blend'
            k = 1
            while os.path.exists(path):
                path = root + '_before_rig_tidy_%d.blend' % k
                k += 1
            try:
                bpy.ops.wm.save_as_mainfile(filepath=path, copy=True)
                msgs.append('Backup: ' + os.path.basename(path))
            except Exception as e:  # add-on save handlers can throw; the copy is usually written
                if os.path.exists(path):
                    msgs.append('Backup: ' + os.path.basename(path))
                else:
                    self.report({'ERROR'}, 'Backup failed: %s' % e)
                    return {'CANCELLED'}

        # 3. state to preserve
        prev_active = context.view_layer.objects.active
        prev_sel = list(context.selected_objects)
        if context.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')
        was_hidden = arm.hide_get()
        arm.hide_set(False)
        for o in prev_sel:
            o.select_set(False)
        context.view_layer.objects.active = arm
        arm.select_set(True)

        context.view_layer.update()
        child_world = {o.name: o.matrix_world.copy() for o in bpy.data.objects
                       if o.parent == arm and o.parent_type == 'BONE'}
        keep = {}
        noise = 0
        for pb in arm.pose.bones:
            if not s.opt_pose or _pose_sig(pb, scale):
                keep[pb.name] = pb.matrix.copy()
            elif pb.matrix_basis != Matrix.Identity(4):
                noise += 1
            pb.matrix_basis = Matrix.Identity(4)
        context.view_layer.update()
        meshes = skinned_meshes(arm)

        def snapshot():
            dg = context.evaluated_depsgraph_get()
            return {o.name: [v.co.copy() for v in o.evaluated_get(dg).data.vertices] for o in meshes}
        before = snapshot()

        # 4. orientation + lower-mouth parenting (one edit-mode pass)
        plan = None
        jaw = jaw_hl = None
        jaw_move = []
        if s.opt_jaw:
            jaw, jaw_hl, jaw_move = jaw_reparent_plan(arm, info)
        top_spine, cl_move = (clavicle_reparent_plan(arm, info) if s.opt_clavicle else (None, []))
        pel_bone, leg_move = (leg_reparent_plan(arm, info) if s.opt_legs else (None, []))
        if s.opt_orient:
            chain_info = {}
            roles = {(i['role'], i['side']): n for n, i in info.items() if i['kind'] != 'generic'}
            pel = roles.get(('pelvis', None)) or roles.get(('com', None))
            hd = roles.get(('head', None))
            body_c = A.bones[pel].head_local.copy() if pel else Vector()
            head_c = A.bones[hd].head_local.copy() if hd else body_c
            for it in s.chains:
                c = head_c if it.category == 'HAIR' or (info.get(it.attach, {}).get('role') in HEAD_ROLES) else body_c
                for n in _chain_bones(it):
                    chain_info[n] = (it.category, c)
            chosen = {}
            for it in s.chains:
                seq = _chain_bones(it)
                for x, y in zip(seq, seq[1:]):
                    chosen[x] = y
            plan = build_plan(arm, info, chosen, chain_info, pts, axes, scale,
                              mirror_pairs(arm, s, left, scale))
        if plan or jaw_move or jaw_hl or cl_move or leg_move:
            heads = {b.name: b.head_local.copy() for b in A.bones}
            mirror = A.use_mirror_x
            A.use_mirror_x = False
            bpy.ops.object.mode_set(mode='EDIT')
            E = A.edit_bones
            conn = {e.name: e.use_connect for e in E}
            for e in E:
                e.use_connect = False
            if plan:
                for e in E:
                    d, L, rv = plan[e.name]
                    e.head = heads[e.name]
                    e.tail = heads[e.name] + d * L
                    e.align_roll(rv)
            if jaw_hl:
                E[jaw].parent = E[jaw_hl]
                conn[jaw] = False
            for n in jaw_move:
                E[n].parent = E[jaw]
                conn[n] = False
            for n in cl_move:
                E[n].parent = E[top_spine]
                conn[n] = False
            for n in leg_move:
                E[n].parent = E[pel_bone]
                conn[n] = False
            for e in E:
                e.head = heads[e.name]
                if conn[e.name] and e.parent and (e.parent.tail - e.head).length < 1e-6 * scale:
                    e.use_connect = True
            bpy.ops.object.mode_set(mode='OBJECT')
            if jaw_move or jaw_hl:
                msgs.append('Parented %d lower-mouth bone(s) to the jaw' % len(jaw_move)
                            + (', jaw to head_lower' if jaw_hl else ''))
            if cl_move:
                msgs.append('Parented %d clavicle(s) to %s' % (len(cl_move), result[top_spine]))
            if leg_move:
                msgs.append('Parented %d thigh(s) to %s' % (len(leg_move), result[pel_bone]))
            A.use_mirror_x = mirror
            context.view_layer.update()
            after = snapshot()
            mx = 0.0
            for name, pts_b in before.items():
                pts_a = after.get(name, [])
                if len(pts_a) == len(pts_b):
                    for p, q in zip(pts_b, pts_a):
                        mx = max(mx, (p - q).length)
            if mx > 1e-4 * scale:
                msgs.append('WARNING: mesh moved by %.5f - check the result' % mx)

        # 5. restore real pose offsets + bone-parented objects
        depth = sorted(keep, key=lambda n: len(arm.pose.bones[n].bone.parent_recursive))
        for n in depth:
            arm.pose.bones[n].matrix = keep[n]
            context.view_layer.update()
        for on, m in child_world.items():
            bpy.data.objects[on].matrix_world = m

        # 6. rename (vertex groups, constraints and parent_bone follow automatically)
        changes = {o: nw for o, nw in result.items() if nw != o}
        for o, nw in changes.items():
            A.bones[o].name = '__rt__' + nw
        for b in A.bones:
            if b.name.startswith('__rt__'):
                b.name = b.name[6:]

        if s.detached:
            s.detached = '\t'.join(result.get(n, n) for n in s.detached.split('\t') if n)

        # 6b. bone collections (only Rig Tidy's own collections are touched)
        if s.opt_collections:
            cat_of = {}
            for it in s.chains:
                for n in _chain_bones(it):
                    cat_of[n] = it.category
            cols = {}
            wanted = list(COLLECTIONS) + CLOTHING_SUBS
            target = {}
            for orig, new in result.items():
                cn = collection_for(info[orig], cat_of.get(orig), new)
                target[orig] = cn
                if cn not in wanted:
                    wanted.append(cn)
            for cn in list(wanted):                      # make sure parents exist too
                par = collection_parent(cn)
                while par and par not in wanted:
                    wanted.insert(0, par)
                    par = collection_parent(par)

            def ensure(cname, depth=0):
                if cname in cols:
                    return cols[cname]
                par_name = collection_parent(cname) if depth < 8 else None
                par = ensure(par_name, depth + 1) if par_name else None
                c = A.collections_all.get(cname)
                if c is None:
                    c = A.collections.new(cname, parent=par) if par else A.collections.new(cname)
                elif par is not None and c.parent != par:
                    c.parent = par
                cols[cname] = c
                return c
            for cname in wanted:
                ensure(cname)
            legacy = [A.collections_all[n] for n in LEGACY_COLLECTIONS
                      if n in A.collections_all and n not in cols]
            counts = {}
            for orig, new in result.items():
                bone = A.bones[new]
                for c in list(cols.values()) + legacy:
                    c.unassign(bone)
                cname = target[orig]
                cols[cname].assign(bone)
                counts[cname] = counts.get(cname, 0) + 1
            # remove empty managed collections, children first; keep parents that still hold children
            for c in legacy:
                if not c.bones and not c.children:
                    A.collections.remove(c)
            def depth(c):
                d = 0
                while c.parent is not None:
                    d += 1
                    c = c.parent
                return d
            for cname in sorted(cols, key=lambda n: -depth(cols[n])):
                c = cols[cname]
                if not c.bones and not c.children:
                    A.collections.remove(c)
            msgs.append('Collections: ' + ', '.join('%s %d' % (c, counts[c])
                                                    for c in wanted if counts.get(c)))

        # 7. tidy up
        arm.select_set(False)
        arm.hide_set(was_hidden)
        for o in prev_sel:
            if o.name in bpy.data.objects:
                o.select_set(True)
        context.view_layer.objects.active = prev_active
        _SUSPEND[0] = True
        s.chains.clear()
        _SUSPEND[0] = False
        s.analyzed = False
        summary = ['Done: %d bones renamed' % len(changes)]
        if swaps:
            summary.append('Fixed %d swapped pair(s)' % len(swaps))
        if noise:
            summary.append('Cleared pose noise on %d bones' % noise)
        if keep and s.opt_pose:
            summary.append('Kept %d real pose offset(s)' % len(keep))
        summary += msgs
        s.report = '\n'.join(summary)
        self.report({'INFO'}, 'Rig Tidy: ' + summary[0])
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------

class RIGTIDY_UL_chains(bpy.types.UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        row = layout.row(align=True)
        row.prop(item, 'selected', text='')
        split = row.split(factor=0.42)
        split.label(text=item.root, icon='BONE_DATA')
        sub = split.row(align=True)
        sub.label(text=item.preview)
        sub.label(text='%d' % item.count)
        if item.testing:
            sub.label(text='', icon='PLAY')


OPTION_GROUPS = [
    ('rig_tidy_opt_naming', 'Naming', ('opt_body', 'opt_face', 'opt_chains', 'opt_swaps',
                                        'suffix_style', 'opt_strip', 'opt_translate')),
    ('rig_tidy_opt_struct', 'Bones & Parenting', ('opt_orient', 'opt_jaw', 'opt_clavicle',
                                                  'opt_legs', 'opt_pose')),
    ('rig_tidy_opt_output', 'Output', ('opt_collections', 'opt_backup')),
]


def _draw_group_props(col, s, props):
    for p in props:
        col.prop(s, p)
        if p == 'opt_strip' and s.opt_strip:
            sub = col.column(align=True)
            sub.prop(s, 'strip_prefix')
            sub.prop(s, 'strip_suffix')
        if p == 'opt_translate' and s.opt_translate:
            row = col.row(align=True)
            row.label(text='Dictionary: %d words' % len(load_dictionary()), icon='TEXT')
            row.operator('rig_tidy.reload_dictionary', text='', icon='FILE_REFRESH')


def _draw_options(lay, s):
    """Collapsible option sections (native sub-panels on Blender 4.1+)."""
    if hasattr(lay, 'panel'):
        header, body = lay.panel('rig_tidy_options', default_closed=True)
        header.label(text='Cleanup Options', icon='PREFERENCES')
        if body is None:
            return
        for pid, title, props in OPTION_GROUPS:
            h, b = body.panel(pid, default_closed=False)
            h.label(text=title)
            if b is not None:
                _draw_group_props(b.column(align=True), s, props)
        return
    box = lay.box()
    row = box.row()
    row.prop(s, 'show_options', icon='TRIA_DOWN' if s.show_options else 'TRIA_RIGHT',
             emboss=False, text='Cleanup Options')
    if s.show_options:
        for pid, title, props in OPTION_GROUPS:
            col = box.column(align=True)
            col.label(text=title)
            _draw_group_props(col, s, props)


class RIGTIDY_PT_main(bpy.types.Panel):
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'Rig Tidy'
    bl_label = 'Rig Tidy'

    @classmethod
    def poll(cls, context):
        return _arm(context) is not None

    def draw(self, context):
        arm = _arm(context)
        s = arm.data.rig_tidy
        lay = self.layout
        lay.operator('rig_tidy.analyze', icon='VIEWZOOM')
        lay.prop(s, 'opt_auto_face')
        if s.report:
            box = lay.box()
            col = box.column(align=True)
            for line in s.report.split('\n'):
                col.label(text=line, icon='ERROR' if line.startswith(('WARNING', 'Swapped', 'Check'))
                          else 'DOT')

        if s.analyzed and len(s.chains):
            row = lay.row()
            row.label(text='Unnamed chains')
            row.prop(s, 'live_select', icon='RESTRICT_SELECT_OFF', toggle=True)
            lay.template_list('RIGTIDY_UL_chains', '', s, 'chains', s, 'index', rows=6)
            row = lay.row(align=True)
            row.operator('rig_tidy.check', text='All').action = 'ALL'
            row.operator('rig_tidy.check', text='None').action = 'NONE'
            row.operator('rig_tidy.check', text='Invert').action = 'INVERT'
            row = lay.row(align=True)
            row.operator('rig_tidy.detach', icon='UNLINKED')
            nd = len([n for n in s.detached.split('\t') if n])
            if nd:
                op = row.operator('rig_tidy.detach', text='', icon='X')
                op.clear = True
            if 0 <= s.index < len(s.chains):
                it = s.chains[s.index]
                box = lay.box()
                col = box.column()
                nsel = sum(1 for x in s.chains if x.selected)
                if it.selected and nsel > 1:
                    col.label(text='Editing %d checked chains' % nsel, icon='LINKED')
                col.prop(it, 'category_ui')
                if it.category != 'KEEP':
                    col.prop(it, 'label')
                    col.row().prop(it, 'side', expand=True)
                info = col.column(align=True)
                info.scale_y = 0.8
                info.label(text='Attached to: %s' % (it.attach or '(nothing - root)'))
                col.prop_search(it, 'partner', s, 'chains', icon='MOD_MIRROR')
                row = col.row(align=True)
                op = row.operator('rig_tidy.select_chain', icon='RESTRICT_SELECT_OFF')
                op.index = s.index
                op = row.operator('rig_tidy.test_chain', icon='PLAY', depress=it.testing)
                op.index = s.index

        if s.analyzed:
            _draw_options(lay, s)
            lay.operator('rig_tidy.apply', icon='CHECKMARK')

        lay.separator()
        active = (arm.name + '#pose') in _TEST_STORE
        lay.operator('rig_tidy.test_pose', icon='ARMATURE_DATA', depress=active,
                     text='Reset Test Pose' if active else 'Test Pose')


# ---------------------------------------------------------------------------
# Preferences (Edit > Preferences > Add-ons > Rig Tidy)
# ---------------------------------------------------------------------------

class RIGTIDY_OT_reload_dictionary(bpy.types.Operator):
    bl_idname = 'rig_tidy.reload_dictionary'
    bl_label = 'Reload Dictionary'
    bl_description = 'Re-read dictionary.csv (and your own dictionary file) and refresh name previews'

    def execute(self, context):
        words = load_dictionary(force=True)
        for a in bpy.data.armatures:
            st = getattr(a, 'rig_tidy', None)
            if st is not None and st.analyzed:
                refresh_previews(st)
        self.report({'INFO'}, 'Rig Tidy dictionary: %d words' % len(words))
        return {'FINISHED'}


class RigTidyCustomCategory(bpy.types.PropertyGroup):
    name: StringProperty(name='Name', default='New Category',
                         description='Shown in the chain category menu')
    token: StringProperty(name='Name Prefix', default='',
                          description='Bones get named <prefix>_01.L ... (empty = use the name)')
    collection: StringProperty(name='Collection', default='Other',
                               description='Bone collection for this category (created if missing)')
    parent: StringProperty(name='Parent Collection', default='',
                           description='Optional: nest the collection under this one, e.g. Clothing '
                                       '(created if missing)')


class RIGTIDY_UL_custom_categories(bpy.types.UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        row = layout.row(align=True)
        row.prop(item, 'name', text='', emboss=False, icon='BOOKMARKS')
        tok = clean_token(item.token or item.name)
        row.label(text='%s_*' % tok if tok else '(no prefix)')
        coll = item.collection or 'Other'
        par = (item.parent or '').strip()
        row.label(text='%s / %s' % (par, coll) if par else coll, icon='GROUP_BONE')


class RIGTIDY_OT_custom_category(bpy.types.Operator):
    bl_idname = 'rig_tidy.custom_category'
    bl_label = 'Custom Category'
    bl_description = 'Add or remove a custom chain category'
    action: EnumProperty(items=[('ADD', 'Add', ''), ('REMOVE', 'Remove', '')])

    def execute(self, context):
        p = get_prefs()
        if p is None:
            self.report({'ERROR'}, 'Rig Tidy preferences not available (is the add-on installed?)')
            return {'CANCELLED'}
        if self.action == 'ADD':
            c = p.custom_categories.add()
            c.name = 'New Category'
            p.custom_index = len(p.custom_categories) - 1
        elif 0 <= p.custom_index < len(p.custom_categories):
            p.custom_categories.remove(p.custom_index)
            p.custom_index = max(0, p.custom_index - 1)
        return {'FINISHED'}


def _prefs_annotations():
    ann = {
        'dictionary_path': StringProperty(
            name='Extra Dictionary', subtype='FILE_PATH',
            description='Optional CSV (word,english) loaded after the built-in dictionary.csv; '
                        'its entries win'),
        'custom_categories': CollectionProperty(type=RigTidyCustomCategory),
        'custom_index': IntProperty(),
    }
    for cid, name, desc in CATEGORY_ITEMS:
        ann['cat_' + cid] = BoolProperty(name=name, default=True, description=desc)
    return ann


class RigTidyPreferences(bpy.types.AddonPreferences):
    bl_idname = __package__ or __name__
    __annotations__ = _prefs_annotations()

    def draw(self, context):
        lay = self.layout
        off = [cid for cid in CORE_CATEGORIES if not getattr(self, 'cat_' + cid, True)]
        if off:
            box = lay.box()
            col = box.column(align=True)
            col.label(text='Core categories disabled - allowed, but note:', icon='ERROR')
            for cid in off:
                name = next(n for i, n, _ in CATEGORY_ITEMS if i == cid)
                col.label(text='  %s: %s' % (name, CORE_CATEGORIES[cid]))

        box = lay.box()
        box.label(text='Name Dictionary', icon='TEXT')
        col = box.column()
        files = dictionary_files()
        n = len(load_dictionary())
        row = col.row()
        row.label(text='Built-in: %s' % os.path.basename(files[0]) +
                  ('' if os.path.isfile(files[0]) else '  (missing)'))
        row.label(text='%d words loaded' % n)
        row.operator('rig_tidy.reload_dictionary', text='', icon='FILE_REFRESH')
        col.prop(self, 'dictionary_path')

        box = lay.box()
        box.label(text='Chain Categories', icon='OUTLINER_DATA_ARMATURE')
        grid = box.grid_flow(columns=3, even_columns=True, align=True)
        for cid, name, desc in CATEGORY_ITEMS:
            grid.prop(self, 'cat_' + cid, text=name + (' *' if cid in CORE_CATEGORIES else ''))
        box.label(text='* core category: turning it off changes how chains are guessed or oriented')

        box.separator()
        box.label(text='Custom Categories (for quick selection - not auto-detected)')
        row = box.row()
        row.template_list('RIGTIDY_UL_custom_categories', '', self, 'custom_categories',
                          self, 'custom_index', rows=3)
        col = row.column(align=True)
        col.operator('rig_tidy.custom_category', text='', icon='ADD').action = 'ADD'
        col.operator('rig_tidy.custom_category', text='', icon='REMOVE').action = 'REMOVE'
        if 0 <= self.custom_index < len(self.custom_categories):
            c = self.custom_categories[self.custom_index]
            sub = box.column(align=True)
            sub.prop(c, 'name')
            sub.prop(c, 'token')
            sub.prop(c, 'collection')
            sub.prop(c, 'parent')


classes = (RIGTIDY_OT_reload_dictionary, RigTidyCustomCategory, RIGTIDY_UL_custom_categories,
           RIGTIDY_OT_custom_category,
           RigTidyChain, RigTidySettings, RIGTIDY_OT_analyze, RIGTIDY_OT_select_chain,
           RIGTIDY_OT_check, RIGTIDY_OT_detach,
           RIGTIDY_OT_test_chain, RIGTIDY_OT_test_pose, RIGTIDY_OT_apply,
           RIGTIDY_UL_chains, RIGTIDY_PT_main)


_LIVE_LAST = {}


@bpy.app.handlers.persistent
def _live_select_handler(scene, depsgraph=None):
    """Live Select: viewport bone selection drives the chain list.
    Every chain with a selected bone gets checked; the active bone's chain is highlighted."""
    try:
        obj = bpy.context.view_layer.objects.active
    except Exception:
        return
    if not obj or obj.type != 'ARMATURE' or obj.mode not in ('POSE', 'EDIT'):
        return
    A = obj.data
    s = A.rig_tidy
    if not s.live_select or not s.analyzed or not len(s.chains):
        return
    act = A.edit_bones.active if obj.mode == 'EDIT' else A.bones.active
    sel = frozenset(_selected_bones(obj))
    key = (sel, act.name if act else None)
    if _LIVE_LAST.get(obj.name) == key:
        return
    _LIVE_LAST[obj.name] = key
    changed = False
    for k, it in enumerate(s.chains):
        bones = set(it.bones.split('\t'))
        want = bool(bones & sel)
        if it.selected != want:
            it.selected = want
            changed = True
        if act and act.name in bones and s.index != k:
            cur = s.chains[s.index] if 0 <= s.index < len(s.chains) else None
            if not (cur and act.name in cur.bones.split('\t')):
                s.index = k
                changed = True
    if changed:
        for win in bpy.context.window_manager.windows:
            for area in win.screen.areas:
                if area.type == 'VIEW_3D':
                    area.tag_redraw()


def register():
    for c in classes:
        bpy.utils.register_class(c)
    if __package__:          # preferences only exist when installed as an add-on
        bpy.utils.register_class(RigTidyPreferences)
    bpy.types.Armature.rig_tidy = PointerProperty(type=RigTidySettings)
    if _live_select_handler not in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.append(_live_select_handler)


def unregister():
    for h in list(bpy.app.handlers.depsgraph_update_post):
        if getattr(h, '__name__', '') == '_live_select_handler':
            bpy.app.handlers.depsgraph_update_post.remove(h)
    if __package__:
        try:
            bpy.utils.unregister_class(RigTidyPreferences)
        except RuntimeError:
            pass
    if hasattr(bpy.types.Armature, 'rig_tidy'):
        del bpy.types.Armature.rig_tidy
    for c in reversed(classes):
        try:
            bpy.utils.unregister_class(c)
        except RuntimeError:
            pass  # already replaced (e.g. the script was re-run from the Text Editor)


if __name__ == '__main__':
    register()
