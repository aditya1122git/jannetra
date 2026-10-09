import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import type { IconDefinition } from "@fortawesome/fontawesome-svg-core";
import { faEye, faEyeSlash, faTableColumns, faTowerBroadcast, faGear, faDownload, faMagnifyingGlass, faArrowUpRightFromSquare, faTriangleExclamation, faChevronRight, faArrowsRotate, faRightFromBracket, faShieldHalved, faCalendarDays, faChartLine, faCheck, faXmark, faBell, faBars, faNewspaper } from "@fortawesome/free-solid-svg-icons";
import { faFacebookF, faInstagram, faRedditAlien, faYoutube, faXTwitter } from "@fortawesome/free-brands-svg-icons";
type Props = { size?: number; className?: string };
function iconComponent(icon: IconDefinition) {
  return function Icon({ size = 20, className }: Props) {
    return <FontAwesomeIcon icon={icon} className={className} style={{ width: size, height: size }} aria-hidden="true" />;
  };
}
export const Eye = iconComponent(faEye);
export const EyeSlash = iconComponent(faEyeSlash);
export const LayoutDashboard = iconComponent(faTableColumns);
export const Radio = iconComponent(faTowerBroadcast);
export const Settings = iconComponent(faGear);
export const Download = iconComponent(faDownload);
export const Search = iconComponent(faMagnifyingGlass);
export const ArrowUpRight = iconComponent(faArrowUpRightFromSquare);
export const TriangleAlert = iconComponent(faTriangleExclamation);
export const ChevronRight = iconComponent(faChevronRight);
export const RefreshCw = iconComponent(faArrowsRotate);
export const LogOut = iconComponent(faRightFromBracket);
export const ShieldCheck = iconComponent(faShieldHalved);
export const CalendarDays = iconComponent(faCalendarDays);
export const Activity = iconComponent(faChartLine);
export const Check = iconComponent(faCheck);
export const Close = iconComponent(faXmark);
export const Bell = iconComponent(faBell);
export const Menu = iconComponent(faBars);
const platforms: Record<string, IconDefinition> = { facebook: faFacebookF, instagram: faInstagram, youtube: faYoutube, x: faXTwitter, news: faNewspaper, reddit: faRedditAlien };
export function PlatformIcon({ platform }: { platform: string }) {
  return <FontAwesomeIcon icon={platforms[platform] || faTowerBroadcast} aria-hidden="true" />;
}
