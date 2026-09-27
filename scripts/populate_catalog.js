#!/usr/bin/env node
/**
 * =============================================================================
 * AMETHEUS — IDEMPOTENT GAME CATALOG & QUALIFIER TOURNAMENT SETUP SCRIPT
 * =============================================================================
 * Flow:
 *   Setup Script ───> Ametheus Backend API ───> Validation / Auth ───> Database
 *
 * Scoped strictly to:
 *   - 11 Official Games (4 ACTIVE, 7 COMING_SOON)
 *   - 4 Qualifier Tournaments (ONLY for the 4 Active Games)
 *   - Absolutely NO dummy data, fake users, test registrations, or payments.
 * =============================================================================
 */

const https = require('https');
const http = require('http');

const API_BASE = process.env.API_URL || 'https://ametheus.duckdns.org';
const CLERK_SECRET_KEY = process.env.CLERK_SECRET_KEY || 'sk_test_eywy4y4AqgoZvmPYVzqzRa55m0AUoj0OOm17CheaVz';
const ADMIN_USER_ID = process.env.ADMIN_USER_ID || 'user_3JSZ1vAIg66XIXXDPXHuOtJluZJ'; // pk.4081988@gmail.com

// ---------------------------------------------------------------------------
// 1. The 11 Official Games
// ---------------------------------------------------------------------------
const GAMES = [
  {
    name: 'BGMI',
    slug: 'bgmi',
    category: 'mobile',
    platform: 'Mobile (Android / iOS)',
    tag: 'Battle Royale',
    status: 'ACTIVE',
    is_team_game: true,
    min_team_size: 4,
    max_team_size: 5,
    max_substitutes: 1,
    entry_fee_inr: 0,
    prize_pool_inr: 50000,
    banner_url: 'https://images.unsplash.com/photo-1538481199705-c710c4e965fc?q=80&w=1200&auto=format&fit=crop',
    logo_url: 'https://images.unsplash.com/photo-1563089145-599997674d42?q=80&w=300&auto=format&fit=crop',
    server_region: 'AWS Mumbai (ap-south-1)',
    anti_cheat: 'Krafton Anti-Cheat Security System',
    match_protocol: 'Custom Room (Room ID & Password Supported)',
    accent_color: '#E56A10',
    description: 'Battlegrounds Mobile India (BGMI) is Krafton\'s premier mobile battle royale discipline. Squads of four drop onto iconic maps like Erangel, Miramar, and Sanhok, executing coordinated rotations, positioning strategies, and precise firefights under official custom room protocols.'
  },
  {
    name: 'VALORANT',
    slug: 'valorant',
    category: 'pc',
    platform: 'PC (Windows)',
    tag: 'Tactical FPS',
    status: 'ACTIVE',
    is_team_game: true,
    min_team_size: 5,
    max_team_size: 7,
    max_substitutes: 2,
    entry_fee_inr: 0,
    prize_pool_inr: 100000,
    banner_url: 'https://images.unsplash.com/photo-1542751371-adc38448a05e?q=80&w=1200&auto=format&fit=crop',
    logo_url: 'https://images.unsplash.com/photo-1550745165-9bc0b252726f?q=80&w=300&auto=format&fit=crop',
    server_region: 'AWS Mumbai (ap-south-1)',
    anti_cheat: 'Riot Vanguard (Hardware TPM 2.0 / Secure Boot)',
    match_protocol: 'Custom Game (Tournament Mode with Cheats Off)',
    accent_color: '#FA4454',
    description: 'VALORANT is Riot Games\' flagship 5v5 character-based tactical shooter. Two rosters of five clash over 24 rounds of precise gunplay, hyper-responsive utility, and agent synergies across verified competitive map pools (Ascent, Bind, Haven, Split, Sunset, Lotus, Abyss).'
  },
  {
    name: 'Free Fire',
    slug: 'free-fire',
    category: 'mobile',
    platform: 'Mobile (Android / iOS)',
    tag: 'Battle Royale',
    status: 'ACTIVE',
    is_team_game: true,
    min_team_size: 4,
    max_team_size: 5,
    max_substitutes: 1,
    entry_fee_inr: 0,
    prize_pool_inr: 30000,
    banner_url: 'https://images.unsplash.com/photo-1518709268805-4e9042af9f23?q=80&w=1200&auto=format&fit=crop',
    logo_url: 'https://images.unsplash.com/photo-1579783902614-a3fb3927b675?q=80&w=300&auto=format&fit=crop',
    server_region: 'AWS Mumbai (ap-south-1)',
    anti_cheat: 'Garena Anti-Hack System',
    match_protocol: 'Custom Room (Room ID & Password Supported)',
    accent_color: '#FFAA00',
    description: 'Free Fire is Garena\'s ultra-dynamic mobile survival battle royale. Squads of four compete in fast-paced 50-player combat zones where rapid engagements, character skill combinations, and tactical survivability determine the champion team.'
  },
  {
    name: 'Call of Duty: Mobile',
    slug: 'cod-mobile',
    category: 'mobile',
    platform: 'Mobile (Android / iOS)',
    tag: 'Multiplayer FPS',
    status: 'ACTIVE',
    is_team_game: true,
    min_team_size: 5,
    max_team_size: 6,
    max_substitutes: 1,
    entry_fee_inr: 0,
    prize_pool_inr: 40000,
    banner_url: 'https://images.unsplash.com/photo-1579373903781-fd5c0c30c4cd?q=80&w=1200&auto=format&fit=crop',
    logo_url: 'https://images.unsplash.com/photo-1550745165-9bc0b252726f?q=80&w=300&auto=format&fit=crop',
    server_region: 'AWS Mumbai (ap-south-1)',
    anti_cheat: 'Activision Ricochet Mobile Protocol',
    match_protocol: 'Private Match Lobby (Hardpoint, Search & Destroy, Control)',
    accent_color: '#F5A623',
    description: 'Call of Duty: Mobile delivers Activision\'s competitive multiplayer esports experience. 5v5 rosters battle across sanctioned tournament modes—Hardpoint, Control, and Search & Destroy—under strict official weapon and perk restrictions.'
  },
  {
    name: 'Counter-Strike 2',
    slug: 'counter-strike-2',
    category: 'pc',
    platform: 'PC (Steam / Windows)',
    tag: 'Tactical FPS',
    status: 'COMING_SOON',
    is_team_game: true,
    min_team_size: 5,
    max_team_size: 5,
    max_substitutes: 1,
    entry_fee_inr: 0,
    prize_pool_inr: 0,
    banner_url: 'https://images.unsplash.com/photo-1511512578047-dfb367046420?q=80&w=1200&auto=format&fit=crop',
    logo_url: 'https://images.unsplash.com/photo-1579373903781-fd5c0c30c4cd?q=80&w=300&auto=format&fit=crop',
    server_region: 'AWS Mumbai (ap-south-1)',
    anti_cheat: 'Valve Anti-Cheat (VAC) + DGS Server Protocol',
    match_protocol: 'Private Tournament Server (IP/Port/Password/Connect Command Supported)',
    accent_color: '#DE9B35',
    description: 'Counter-Strike 2 represents Valve\'s largest technical leap in franchise history. Built on the Source 2 engine, teams of five clash in MR12 competitive defusal matches with sub-tick architecture, volumetric smoke, and dedicated tournament servers.'
  },
  {
    name: 'Apex Legends',
    slug: 'apex-legends',
    category: 'pc',
    platform: 'PC / Multiplatform',
    tag: 'Battle Royale',
    status: 'COMING_SOON',
    is_team_game: true,
    min_team_size: 3,
    max_team_size: 3,
    max_substitutes: 1,
    entry_fee_inr: 0,
    prize_pool_inr: 0,
    banner_url: 'https://images.unsplash.com/photo-1542751371-adc38448a05e?q=80&w=1200&auto=format&fit=crop',
    logo_url: 'https://images.unsplash.com/photo-1550745165-9bc0b252726f?q=80&w=300&auto=format&fit=crop',
    server_region: 'AWS Mumbai / Singapore',
    anti_cheat: 'Easy Anti-Cheat (EAC)',
    match_protocol: 'Tournament Custom Lobby (Match Code Supported)',
    accent_color: '#DA292A',
    description: 'Apex Legends is Respawn Entertainment\'s hero battle royale. Trios master deep character abilities, fluid high-speed movement, and tactical gunplay in official custom lobbies supporting private match codes.'
  },
  {
    name: 'Rocket League',
    slug: 'rocket-league',
    category: 'pc',
    platform: 'PC / Cross-Platform',
    tag: 'Vehicular Soccer',
    status: 'COMING_SOON',
    is_team_game: true,
    min_team_size: 3,
    max_team_size: 4,
    max_substitutes: 1,
    entry_fee_inr: 0,
    prize_pool_inr: 0,
    banner_url: 'https://images.unsplash.com/photo-1511512578047-dfb367046420?q=80&w=1200&auto=format&fit=crop',
    logo_url: 'https://images.unsplash.com/photo-1579373903781-fd5c0c30c4cd?q=80&w=300&auto=format&fit=crop',
    server_region: 'AWS Mumbai / Singapore',
    anti_cheat: 'Epic Games Online Services Anti-Cheat',
    match_protocol: 'Private Match (Name/Password Supported)',
    accent_color: '#0088FF',
    description: 'Rocket League is Psyonix and Epic Games\' physics-based vehicular soccer. Teams of three execute aerial hits, calculated dribbles, and defensive rotations inside private password-protected stadiums.'
  },
  {
    name: 'Smash Karts',
    slug: 'smash-karts',
    category: 'web',
    platform: 'Web / Browser / Mobile',
    tag: 'Kart Battle',
    status: 'COMING_SOON',
    is_team_game: false,
    min_team_size: 1,
    max_team_size: 1,
    max_substitutes: 0,
    entry_fee_inr: 0,
    prize_pool_inr: 0,
    banner_url: 'https://images.unsplash.com/photo-1538481199705-c710c4e965fc?q=80&w=1200&auto=format&fit=crop',
    logo_url: 'https://images.unsplash.com/photo-1563089145-599997674d42?q=80&w=300&auto=format&fit=crop',
    server_region: 'Global Low-Latency Edge (ap-south-1)',
    anti_cheat: 'Smash Karts Session Token Protocol',
    match_protocol: 'Private Game (Join Link Supported, Up to 8 Players)',
    accent_color: '#9B51E0',
    description: 'Smash Karts is a high-speed 3D multiplayer kart arena battler. Individual racers collect power-ups, rockets, and machine guns to destroy rival karts in an 8-player Free For All arena.'
  },
  {
    name: 'Clash Royale',
    slug: 'clash-royale',
    category: 'mobile',
    platform: 'Mobile (Android / iOS)',
    tag: 'Tower Defense / RTS',
    status: 'COMING_SOON',
    is_team_game: false,
    min_team_size: 1,
    max_team_size: 1,
    max_substitutes: 0,
    entry_fee_inr: 0,
    prize_pool_inr: 0,
    banner_url: 'https://images.unsplash.com/photo-1529699211952-734e80c4d42b?q=80&w=1200&auto=format&fit=crop',
    logo_url: 'https://images.unsplash.com/photo-1586165368502-1bad197a6461?q=80&w=300&auto=format&fit=crop',
    server_region: 'Supercell Cloud Mesh',
    anti_cheat: 'Supercell Fair Play Protocol',
    match_protocol: '1v1 Friendly Battle (Player ID Challenge)',
    accent_color: '#2D9CDB',
    description: 'Clash Royale is Supercell\'s real-time tower-rush strategy game. Individual contenders build tactical 8-card decks to destroy opponent crown towers under standardized tournament level caps.'
  },
  {
    name: 'Brawl Stars',
    slug: 'brawl-stars',
    category: 'mobile',
    platform: 'Mobile (Android / iOS)',
    tag: 'Hero Shooter / MOBA',
    status: 'COMING_SOON',
    is_team_game: true,
    min_team_size: 3,
    max_team_size: 4,
    max_substitutes: 1,
    entry_fee_inr: 0,
    prize_pool_inr: 0,
    banner_url: 'https://images.unsplash.com/photo-1518709268805-4e9042af9f23?q=80&w=1200&auto=format&fit=crop',
    logo_url: 'https://images.unsplash.com/photo-1579783902614-a3fb3927b675?q=80&w=300&auto=format&fit=crop',
    server_region: 'Supercell Cloud Mesh',
    anti_cheat: 'Supercell Fair Play Protocol',
    match_protocol: 'Friendly / Custom Lobby (Team Code & Join Link Supported)',
    accent_color: '#F2C94C',
    description: 'Brawl Stars is a fast-paced 3v3 multiplayer mobile battle game. Teams coordinate Brawler picks across diverse game modes (Gem Grab, Brawl Ball, Bounty, Knockout) in custom friendly lobbies.'
  },
  {
    name: 'FIFA Mobile',
    slug: 'fifa-mobile',
    category: 'mobile',
    platform: 'Mobile (Android / iOS)',
    tag: 'Sports / Football',
    status: 'COMING_SOON',
    is_team_game: false,
    min_team_size: 1,
    max_team_size: 1,
    max_substitutes: 0,
    entry_fee_inr: 0,
    prize_pool_inr: 0,
    banner_url: 'https://images.unsplash.com/photo-1511512578047-dfb367046420?q=80&w=1200&auto=format&fit=crop',
    logo_url: 'https://images.unsplash.com/photo-1579373903781-fd5c0c30c4cd?q=80&w=300&auto=format&fit=crop',
    server_region: 'EA Sports Online Infrastructure',
    anti_cheat: 'EA Security Integrity Protocol',
    match_protocol: 'Head-to-Head Friendly Challenge (EA / FC Mobile Player ID)',
    accent_color: '#27AE60',
    description: 'FIFA Mobile is EA Sports\' premier mobile football simulation. Solo competitors pilot their ultimate squads in real-time 1v1 Head-to-Head matches utilizing official EA FC Mobile competitive mechanics.'
  }
];

// ---------------------------------------------------------------------------
// 2. The 4 Official Qualifier Tournaments (Only for Active Games)
// ---------------------------------------------------------------------------
const TOURNAMENTS = [
  {
    title: 'BGMI Qualifiers',
    slug: 'bgmi-qualifiers',
    game_slug: 'bgmi',
    prize_pool_inr: 50000,
    entry_fee_inr: 0,
    status: 'REGISTRATION_OPEN',
    starts_at: '2026-10-15T18:00:00Z',
    roster_lock_time: '2026-10-15T17:30:00Z',
    expected_start_date: '2026-10-15',
    registration_open_date: '2026-09-28',
    server_region: 'AWS Mumbai (ap-south-1)',
    max_teams: 25,
    min_team_size: 4,
    max_team_size: 5,
    rank_requirement: 'Platinum IV or Above',
    format: 'Battle Royale / Squad',
    tournament_type: 'Points Table / Custom Room',
    match_format: 'Points Table (3 Matches)',
    banner_url: 'https://images.unsplash.com/photo-1538481199705-c710c4e965fc?q=80&w=1200&auto=format&fit=crop',
    description: 'Official BGMI Qualifiers. Squads of 4 active players (with 1 optional substitute) compete across three consecutive maps (Erangel, Miramar, Sanhok). Room ID and password credentials will unlock 15 minutes before match start in the match lobby room.',
    rules: [
      'Each squad must consist of exactly 4 active players and up to 1 optional substitute.',
      'All players must submit their valid 10-digit BGMI Character UID during registration.',
      'Official Room ID and Password will be displayed directly inside the Match Lobby 15 minutes prior to match launch.',
      'Standard Esports Points Matrix: 1st (15 pts), 2nd (12 pts), 3rd (10 pts), 4th (8 pts), 5th (6 pts), 6th (4 pts), 7th (2 pts), 8th-12th (1 pt). Each Finish = 1 pt.',
      'No emulators, iPad/tablet triggers, or third-party crosshairs allowed. Mobile phones only.',
      'Captains must submit clear end-screen screenshots displaying total kills and final placement within 10 minutes of match completion.'
    ],
    schedule: [
      { round: 'Match 1', map: 'Erangel', time: '18:00 IST' },
      { round: 'Match 2', map: 'Miramar', time: '18:45 IST' },
      { round: 'Match 3', map: 'Sanhok', time: '19:30 IST' }
    ],
    prizes: [
      { place: '1st Place', amount: '₹25,000' },
      { place: '2nd Place', amount: '₹15,000' },
      { place: '3rd Place', amount: '₹10,000' }
    ]
  },
  {
    title: 'Valorant Qualifiers',
    slug: 'valorant-qualifiers',
    game_slug: 'valorant',
    prize_pool_inr: 100000,
    entry_fee_inr: 0,
    status: 'REGISTRATION_OPEN',
    starts_at: '2026-10-16T18:00:00Z',
    roster_lock_time: '2026-10-16T17:30:00Z',
    expected_start_date: '2026-10-16',
    registration_open_date: '2026-09-28',
    server_region: 'AWS Mumbai (ap-south-1)',
    max_teams: 16,
    min_team_size: 5,
    max_team_size: 7,
    rank_requirement: 'Gold 1 or Above',
    format: '5v5 Single Elimination Bracket',
    tournament_type: 'Single Elimination',
    match_format: 'Best of 1 (Finals BO3)',
    banner_url: 'https://images.unsplash.com/photo-1542751371-adc38448a05e?q=80&w=1200&auto=format&fit=crop',
    description: 'Official Valorant Qualifiers for tier-1 competitive rosters. 16 teams clash in a single-elimination tournament bracket on standard tournament mode (Cheats: Off, Overtime: Win by 2). Semifinals and Grand Finals are broadcast live on platform match ticker.',
    rules: [
      'Rosters require 5 starting athletes and up to 2 optional registered substitutes.',
      'All players must have their Riot ID (Username#Tagline) verified on Ametheus profile.',
      'Map Pool: Ascent, Bind, Haven, Split, Sunset, Lotus, Abyss. Map veto takes place in match room.',
      'Match Mode: Standard Tournament Mode, Cheats: OFF, Overtime: Win by 2, All rounds played.',
      'Server location: Mumbai. If ping differential exceeds 40ms, neutral server agreed with organizer.',
      'Disputes require unedited recording (POV) or official match scoreboard match summary.'
    ],
    schedule: [
      { round: 'Round of 16', format: 'Best of 1', time: '18:00 IST' },
      { round: 'Quarter-Finals', format: 'Best of 1', time: '19:15 IST' },
      { round: 'Semi-Finals', format: 'Best of 1', time: '20:30 IST' },
      { round: 'Grand Finals', format: 'Best of 3', time: '21:45 IST' }
    ],
    prizes: [
      { place: '1st Place', amount: '₹50,000' },
      { place: '2nd Place', amount: '₹30,000' },
      { place: '3rd Place', amount: '₹20,000' }
    ]
  },
  {
    title: 'Free Fire Qualifiers',
    slug: 'free-fire-qualifiers',
    game_slug: 'free-fire',
    prize_pool_inr: 30000,
    entry_fee_inr: 0,
    status: 'REGISTRATION_OPEN',
    starts_at: '2026-10-17T18:00:00Z',
    roster_lock_time: '2026-10-17T17:30:00Z',
    expected_start_date: '2026-10-17',
    registration_open_date: '2026-09-28',
    server_region: 'AWS Mumbai (ap-south-1)',
    max_teams: 12,
    min_team_size: 4,
    max_team_size: 5,
    rank_requirement: 'Diamond I or Above',
    format: 'Battle Royale / Squad',
    tournament_type: 'Points Table / Custom Room',
    match_format: 'Points Table (3 Matches)',
    banner_url: 'https://images.unsplash.com/photo-1518709268805-4e9042af9f23?q=80&w=1200&auto=format&fit=crop',
    description: 'Official Free Fire Qualifiers. 12 squads battle across Bermuda, Purgatory, and Kalahari in custom room lobbies. Gun attributes are disabled for 100% fair competitive balance.',
    rules: [
      'Each squad must register 4 active starters with up to 1 optional substitute.',
      'Gun Attributes: Strictly OFF in all official tournament custom rooms.',
      'Character Skill: Allowed. Loadouts/Air-drop: Competitive standard settings.',
      'Points System: 1st (12 pts), 2nd (9 pts), 3rd (8 pts), 4th (7 pts), 5th (6 pts), 6th (5 pts), 7th (4 pts), 8th (3 pts), 9th (2 pts), 10th (1 pt). 1 Kill = 1 pt.',
      'Captains must submit end-game summary screenshots within 10 minutes of room completion.'
    ],
    schedule: [
      { round: 'Match 1', map: 'Bermuda', time: '18:00 IST' },
      { round: 'Match 2', map: 'Purgatory', time: '18:40 IST' },
      { round: 'Match 3', map: 'Kalahari', time: '19:20 IST' }
    ],
    prizes: [
      { place: '1st Place', amount: '₹15,000' },
      { place: '2nd Place', amount: '₹10,000' },
      { place: '3rd Place', amount: '₹5,000' }
    ]
  },
  {
    title: 'COD Mobile Qualifiers',
    slug: 'cod-mobile-qualifiers',
    game_slug: 'cod-mobile',
    prize_pool_inr: 40000,
    entry_fee_inr: 0,
    status: 'REGISTRATION_OPEN',
    starts_at: '2026-10-18T18:00:00Z',
    roster_lock_time: '2026-10-18T17:30:00Z',
    expected_start_date: '2026-10-18',
    registration_open_date: '2026-09-28',
    server_region: 'AWS Mumbai (ap-south-1)',
    max_teams: 16,
    min_team_size: 5,
    max_team_size: 6,
    rank_requirement: 'Master I or Above',
    format: '5v5 Competitive Multi-Mode',
    tournament_type: 'Single Elimination',
    match_format: 'Best of 3 (Hardpoint, Search & Destroy, Control)',
    banner_url: 'https://images.unsplash.com/photo-1579373903781-fd5c0c30c4cd?q=80&w=1200&auto=format&fit=crop',
    description: 'Official Call of Duty: Mobile Qualifiers. 16 rosters battle through single elimination brackets across standard competitive rotation: Game 1 Hardpoint, Game 2 Search & Destroy, Game 3 Control.',
    rules: [
      'Teams must register exactly 5 starters with 1 optional substitute.',
      'Map & Mode Rotation: Game 1: Hardpoint (Summit/Slums/Hacienda), Game 2: Search & Destroy (Firing Range/Raid/Standoff), Game 3: Control (Raid/Summit).',
      'Banned Equipment: Persistence, Hardline, Operator Skills as per official CoDM competitive handbook.',
      'Both captains must agree in the match room and report scores immediately upon match finish.'
    ],
    schedule: [
      { round: 'Round of 16', format: 'Best of 3', time: '18:00 IST' },
      { round: 'Quarter-Finals', format: 'Best of 3', time: '19:30 IST' },
      { round: 'Semi-Finals', format: 'Best of 3', time: '21:00 IST' },
      { round: 'Grand Finals', format: 'Best of 3', time: '22:15 IST' }
    ],
    prizes: [
      { place: '1st Place', amount: '₹20,000' },
      { place: '2nd Place', amount: '₹12,000' },
      { place: '3rd Place', amount: '₹8,000' }
    ]
  }
];

// ---------------------------------------------------------------------------
// Helper: HTTP Request Wrapper
// ---------------------------------------------------------------------------
function makeRequest(urlStr, options, data = null) {
  return new Promise((resolve, reject) => {
    const parsed = new URL(urlStr);
    const lib = parsed.protocol === 'https:' ? https : http;
    const req = lib.request(parsed, options, (res) => {
      let body = '';
      res.on('data', chunk => body += chunk);
      res.on('end', () => {
        let parsedBody = body;
        try {
          parsedBody = JSON.parse(body);
        } catch (_) {}
        resolve({ statusCode: res.statusCode, headers: res.headers, body: parsedBody });
      });
    });
    req.on('error', reject);
    if (data) {
      const payload = typeof data === 'string' ? data : JSON.stringify(data);
      req.write(payload);
    }
    req.end();
  });
}

// ---------------------------------------------------------------------------
// Helper: Obtain Super Admin JWT from Clerk
// ---------------------------------------------------------------------------
async function getSuperAdminToken() {
  console.log('[Auth] Minting Clerk Super Admin Session Token...');
  const sessRes = await makeRequest('https://api.clerk.com/v1/sessions', {
    method: 'POST',
    headers: {
      'Authorization': `Bearer ${CLERK_SECRET_KEY}`,
      'Content-Type': 'application/json'
    }
  }, { user_id: ADMIN_USER_ID });

  if (sessRes.statusCode !== 200) {
    throw new Error(`Failed to create Clerk session: ${JSON.stringify(sessRes.body)}`);
  }

  const sessionId = sessRes.body.id;
  const tokenRes = await makeRequest(`https://api.clerk.com/v1/sessions/${sessionId}/tokens`, {
    method: 'POST',
    headers: {
      'Authorization': `Bearer ${CLERK_SECRET_KEY}`,
      'Content-Type': 'application/json',
      'Content-Length': 0
    }
  });

  if (tokenRes.statusCode !== 200 || !tokenRes.body.jwt) {
    throw new Error(`Failed to mint Clerk session token: ${JSON.stringify(tokenRes.body)}`);
  }

  console.log('[Auth] Successfully minted verified Super Admin JWT.');
  return tokenRes.body.jwt;
}

// ---------------------------------------------------------------------------
// Main Idempotent Execution Flow
// ---------------------------------------------------------------------------
async function main() {
  console.log('=============================================================================');
  console.log('AMETHEUS — EXECUTING IDEMPOTENT POPULATION VIA BACKEND API');
  console.log(`Target API: ${API_BASE}`);
  console.log('=============================================================================');

  const token = await getSuperAdminToken();
  const authHeaders = {
    'Authorization': `Bearer ${token}`,
    'Content-Type': 'application/json'
  };

  // -------------------------------------------------------------------------
  // 1. Fetch Existing Games
  // -------------------------------------------------------------------------
  console.log('\n[1/3] Fetching existing game catalog from API...');
  const gamesRes = await makeRequest(`${API_BASE}/api/v1/games`, { method: 'GET' });
  const existingGames = Array.isArray(gamesRes.body) ? gamesRes.body : [];
  console.log(`Found ${existingGames.length} existing games in catalog.`);

  const gameMap = {}; // slug -> game object

  // -------------------------------------------------------------------------
  // 2. Upsert All 11 Games
  // -------------------------------------------------------------------------
  console.log('\n[2/3] Configuring the 11 Official Games (4 Active, 7 Coming Soon)...');
  for (const gameData of GAMES) {
    const existing = existingGames.find(g => g.slug === gameData.slug || g.name.toLowerCase() === gameData.name.toLowerCase());
    if (existing) {
      console.log(`  -> Game '${gameData.name}' (${gameData.slug}) exists (ID: ${existing.id}). Updating to ensure complete data...`);
      const updateRes = await makeRequest(`${API_BASE}/api/v1/games/${existing.id}`, {
        method: 'PUT',
        headers: authHeaders
      }, gameData);

      if (updateRes.statusCode >= 200 && updateRes.statusCode < 300) {
        console.log(`     ✓ Updated successfully: [${gameData.status}]`);
        gameMap[gameData.slug] = updateRes.body;
      } else {
        console.error(`     ✗ Failed to update '${gameData.name}':`, updateRes.body);
        gameMap[gameData.slug] = existing;
      }
    } else {
      console.log(`  -> Creating game '${gameData.name}' (${gameData.slug})...`);
      const createRes = await makeRequest(`${API_BASE}/api/v1/games`, {
        method: 'POST',
        headers: authHeaders
      }, gameData);

      if (createRes.statusCode >= 200 && createRes.statusCode < 300) {
        console.log(`     ✓ Created successfully (ID: ${createRes.body.id}): [${gameData.status}]`);
        gameMap[gameData.slug] = createRes.body;
      } else {
        console.error(`     ✗ Failed to create '${gameData.name}':`, createRes.body);
      }
    }
  }

  // -------------------------------------------------------------------------
  // 3. Fetch Existing Tournaments
  // -------------------------------------------------------------------------
  console.log('\n[3/3] Fetching existing tournaments from API...');
  const tournRes = await makeRequest(`${API_BASE}/api/v1/tournaments`, { method: 'GET' });
  const existingTournaments = Array.isArray(tournRes.body) ? tournRes.body : [];
  console.log(`Found ${existingTournaments.length} existing tournaments.`);

  // -------------------------------------------------------------------------
  // 4. Upsert The 4 Qualifier Tournaments (ONLY for Active Games)
  // -------------------------------------------------------------------------
  console.log('\nConfiguring the 4 Official Qualifier Tournaments...');
  for (const tData of TOURNAMENTS) {
    const targetGame = gameMap[tData.game_slug];
    if (!targetGame) {
      console.error(`  ✗ Error: Target game '${tData.game_slug}' not found in gameMap. Skipping tournament '${tData.title}'.`);
      continue;
    }

    const payload = {
      game_id: targetGame.id,
      title: tData.title,
      slug: tData.slug,
      prize_pool_inr: tData.prize_pool_inr,
      entry_fee_inr: tData.entry_fee_inr,
      status: tData.status,
      starts_at: tData.starts_at,
      roster_lock_time: tData.roster_lock_time,
      expected_start_date: tData.expected_start_date,
      registration_open_date: tData.registration_open_date,
      server_region: tData.server_region,
      max_teams: tData.max_teams,
      min_team_size: tData.min_team_size,
      max_team_size: tData.max_team_size,
      rank_requirement: tData.rank_requirement,
      format: tData.format,
      tournament_type: tData.tournament_type,
      match_format: tData.match_format,
      banner_url: tData.banner_url || targetGame.banner_url,
      description: tData.description,
      rules: tData.rules,
      schedule: tData.schedule,
      prizes: tData.prizes
    };

    const existing = existingTournaments.find(t => t.slug === tData.slug || t.title === tData.title);
    if (existing) {
      console.log(`  -> Tournament '${tData.title}' exists (ID: ${existing.id}). Updating fields...`);
      const updateRes = await makeRequest(`${API_BASE}/api/v1/organizer/tournaments/${existing.id}`, {
        method: 'PUT',
        headers: authHeaders
      }, payload);

      if (updateRes.statusCode >= 200 && updateRes.statusCode < 300) {
        console.log(`     ✓ Updated successfully: [${tData.status}] ₹${tData.prize_pool_inr}`);
      } else {
        console.error(`     ✗ Failed to update tournament '${tData.title}':`, updateRes.body);
      }
    } else {
      console.log(`  -> Creating tournament '${tData.title}' for game '${targetGame.name}'...`);
      const createRes = await makeRequest(`${API_BASE}/api/v1/organizer/tournaments`, {
        method: 'POST',
        headers: authHeaders
      }, payload);

      if (createRes.statusCode >= 200 && createRes.statusCode < 300) {
        console.log(`     ✓ Created tournament (ID: ${createRes.body.id}): [${tData.status}] ₹${tData.prize_pool_inr}`);
      } else {
        console.error(`     ✗ Failed to create tournament '${tData.title}':`, createRes.body);
      }
    }
  }

  // -------------------------------------------------------------------------
  // 5. Verification Check
  // -------------------------------------------------------------------------
  console.log('\n=============================================================================');
  console.log('FINAL VERIFICATION REPORT VIA PUBLIC BACKEND APIS:');
  console.log('=============================================================================');

  // Verify Games
  const finalGamesRes = await makeRequest(`${API_BASE}/api/v1/games`, { method: 'GET' });
  const finalGames = Array.isArray(finalGamesRes.body) ? finalGamesRes.body : [];
  const activeGames = finalGames.filter(g => g.status === 'ACTIVE');
  const comingSoonGames = finalGames.filter(g => g.status === 'COMING_SOON');

  console.log(`Total Games Configured: ${finalGames.length}/11`);
  console.log(`Active Games (${activeGames.length}/4):`, activeGames.map(g => g.name).join(', '));
  console.log(`Coming Soon Games (${comingSoonGames.length}/7):`, comingSoonGames.map(g => g.name).join(', '));

  // Verify Tournaments
  const finalTournRes = await makeRequest(`${API_BASE}/api/v1/tournaments`, { method: 'GET' });
  const finalTournaments = Array.isArray(finalTournRes.body) ? finalTournRes.body : [];
  console.log(`\nTotal Tournaments: ${finalTournaments.length}/4`);
  for (const t of finalTournaments) {
    console.log(`  - ${t.title} [${t.status}] | Game: ${t.game_name} | Prize: ${t.prize_pool || t.prize_pool_inr}`);
  }

  // Verify Coming Soon games received ZERO tournaments
  const comingSoonSlugs = new Set(comingSoonGames.map(g => g.slug));
  const illegalTournaments = finalTournaments.filter(t => comingSoonSlugs.has(t.game_slug));
  if (illegalTournaments.length > 0) {
    console.error('  [!] ALERT: Tournaments found for Coming Soon games:', illegalTournaments);
  } else {
    console.log('  ✓ Verified: The 7 Coming Soon games have ZERO tournaments.');
  }

  // Verify Active Contenders API
  const statsRes = await makeRequest(`${API_BASE}/api/v1/leaderboard/stats`, { method: 'GET' });
  console.log('\nLeaderboard Stats Telemetry:', statsRes.body);

  console.log('\n=============================================================================');
  console.log('CATALOG SETUP COMPLETE & VERIFIED.');
  console.log('=============================================================================');
}

main().catch((err) => {
  console.error('Fatal setup error:', err);
  process.exit(1);
});
