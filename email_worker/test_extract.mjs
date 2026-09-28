import {
  extractInvite,
  extractReset,
  extractOtp,
  classifyTidal,
  uniqueUpnToken,
} from "./src/worker.js";

const LONG_UPN =
  "A".repeat(160) + "BBCdefGhIJKlmnOPQrstuvWxyz0123456789";
const SHORT_UPN = "shortLogo12";
const cta =
  `https://ablink.info.tidal.com/ls/click?upn=${LONG_UPN}`;
const logo =
  `https://ablink.info.tidal.com/ls/click?upn=${SHORT_UPN}`;
const pixel =
  `https://ablink.info.tidal.com/wf/open?upn=${LONG_UPN}`;
const resetDirect =
  "https://login.tidal.com/resetpass/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee";
const resetCta =
  `https://ablink.info.tidal.com/ls/click?upn=${LONG_UPN}xxreset`;

const inviteHtml = `
<a href="${logo}"><img src="${pixel}" alt="TIDAL"></a>
<p>Te han invitado al plan Family</p>
<a href="${cta}">Únete al plan Family</a>
`;

const inviteQp = `
<a href=3D"${logo}"><img src=3D"${pixel}"></a>
<a href=3D"https://ablink.info.tidal.com/ls/click?upn=${LONG_UPN.slice(0, 80)}=
${LONG_UPN.slice(80)}">Join the Family plan</a>
`;

const resetHtml = `
<a href="${logo}"><img alt="TIDAL"></a>
Restablecer tu contraseña Tidal
<a href="${resetDirect}">Restablecer contraseña</a>
<a href="${resetCta}">Reset your password</a>
`;

let failed = 0;
function check(name, got, pred) {
  const ok = typeof pred === "function" ? pred(got) : got === pred;
  if (!ok) {
    failed += 1;
    console.log(`FAIL ${name}\n  got: ${String(got).slice(0, 180)}`);
  } else {
    console.log(`ok   ${name}`);
  }
}

check("invite picks CTA not logo", extractInvite(inviteHtml), cta);
check("invite CTA upn is long", uniqueUpnToken(extractInvite(inviteHtml) || ""), (n) => n.length >= 140);
check("invite QP stitch + CTA", extractInvite(inviteQp), (u) => (u || "").includes(LONG_UPN) && (u || "").includes("/ls/click"));
check("invite ignores pixel", extractInvite(inviteHtml), (u) => u && !u.includes("/wf/open"));
check("reset prefers login.tidal.com/resetpass", extractReset(resetHtml), resetDirect);
check("reset not logo", extractReset(resetHtml), (u) => u && !u.includes(SHORT_UPN));

const deleteHtml = `
<a href="${logo}"><img alt="TIDAL"></a>
Confirm you want to delete your account
To confirm the deletion of your account, enter the code below in the TIDAL app.
12680
The code expires after 3 minutes.
<p>Join the TIDAL Family plan</p>
`;
const deleteDecoded = {
  headers: { subject: "Verify TIDAL account deletion" },
  body: deleteHtml,
  all: deleteHtml,
};
const deleteItem = classifyTidal(deleteDecoded, "cliente-0791@cheapmusic.best");
check("delete OTP is 12680", extractOtp(deleteHtml), "12680");
check("delete mail kind=delete", deleteItem && deleteItem.kind, "delete");
check("delete mail value=12680", deleteItem && deleteItem.value, "12680");

const loginHtml = `
Tu código de inicio de sesión en Tidal — 108111
<p>TIDAL Family</p>
<a href="${logo}">logo</a>
`;
const loginItem = classifyTidal(
  { headers: { subject: "Tu código de inicio de sesión en Tidal" }, body: loginHtml, all: loginHtml },
  "cliente-0791@cheapmusic.best"
);
check("login OTP not stolen as invite", loginItem && loginItem.kind, "login");
check("login OTP value", loginItem && loginItem.value, "108111");

const inviteDecoded = {
  headers: { subject: "Has recibido una invitación para unirte a un plan TIDAL Family" },
  body: inviteHtml,
  all: inviteHtml,
};
const inviteItem = classifyTidal(inviteDecoded, "cliente-0791@cheapmusic.best");
check("invite still kind=invite", inviteItem && inviteItem.kind, "invite");
check("invite still CTA", inviteItem && inviteItem.value, cta);

const familyBienven = `
Has recibido una invitación para unirte a un plan TIDAL Family
Te damos la bienvenida a Family
10003
<a href="${logo}"><img alt="TIDAL"></a>
<a href="${cta}">Únete al plan Family</a>
`;
const familyItem = classifyTidal(
  {
    headers: { subject: "Has recibido una invitación para unirte a un plan TIDAL Family" },
    body: familyBienven,
    all: familyBienven,
  },
  "cliente-0799@cheapmusic.best"
);
check("family invite not stolen as register", familyItem && familyItem.kind, "invite");
check("family invite stores CTA not 10003", familyItem && familyItem.value, cta);

const resetQuery =
  "https://login.tidal.com/resetpass?user=cliente-0804@cheapmusic.best&lid=abc";
const resetMailHtml = `
<a href="${logo}"><img alt="TIDAL"></a>
Restablecer tu contraseña Tidal
<p>Use this link to reset your password</p>
<a href="${resetQuery}">Reset your password</a>
<a href="${cta}">not the family CTA</a>
<p>TIDAL Family plan</p>
`;
check("extractReset catches resetpass?user=", extractReset(resetMailHtml), (u) => (u || "").includes("resetpass") && (u || "").includes("cliente-0804"));
const resetItem = classifyTidal(
  { headers: { subject: "Restablecer tu contraseña Tidal" }, body: resetMailHtml, all: resetMailHtml },
  "cliente-0804@cheapmusic.best"
);
check("reset mail is kind=reset not invite", resetItem && resetItem.kind, "reset");
check("reset mail value is resetpass", resetItem && String(resetItem.value || "").includes("resetpass"), true);

if (failed) {
  console.log(`\n${failed} fallo(s)`);
  process.exit(1);
}
console.log("\nOK");
