// Wheel installation layout (PEP 427).

/**
 * Where a wheel member goes, relative to the environment prefix. Members of the
 * `<dist>.data/<scheme>/` directories are relocated (data files such as Jupyter templates
 * go to the prefix, scripts to bin/); everything else lands in site-packages.
 */
export function wheelPath(member: string, sitePackages: string): string {
  const m = member.match(/^[^/]+\.data\/(purelib|platlib|data|scripts|headers)\/(.+)$/);
  if (!m) {
    return `${sitePackages}/${member}`;
  }
  switch (m[1]) {
    case 'purelib':
    case 'platlib':
      return `${sitePackages}/${m[2]}`;
    case 'data':
      return m[2];
    case 'scripts':
      return `bin/${m[2]}`;
    default:
      return `include/${m[2]}`;
  }
}
