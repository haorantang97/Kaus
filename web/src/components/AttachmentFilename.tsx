/** Keep the file type readable when a composer attachment name is ellipsized. */
export function AttachmentFilename({ name }: { name: string }) {
  const compound = /\.tar\.(?:gz|bz2|xz)$/i.exec(name);
  const split = compound?.index ?? name.lastIndexOf(".");
  const hasExtension = split > 0 && split < name.length - 1;
  return <span className="kaus-attachment-filename"><span>{hasExtension ? name.slice(0, split) : name}</span>{hasExtension && <span>{name.slice(split)}</span>}</span>;
}
